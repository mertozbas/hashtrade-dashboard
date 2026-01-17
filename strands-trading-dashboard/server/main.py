import asyncio, json, time, uuid, os, sys
from dataclasses import dataclass
from typing import Any, Dict, Optional

import websockets

from devduck import DevDuck

# Add tools directory to path so we can import ccxt_tool directly
_proj_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_tools_dir = os.path.join(_proj_root, "tools")
if _tools_dir not in sys.path:
    sys.path.insert(0, _tools_dir)

# Import ccxt_generic directly for UI actions (bypasses agent wrapper)
try:
    import ccxt
except ImportError:
    ccxt = None


# Message envelope matches websocket_ref.py
@dataclass
class StreamMsg:
    type: str
    turn_id: str
    timestamp: float
    data: Any = ""
    meta: Optional[Dict[str, Any]] = None

    def dumps(self) -> str:
        payload = {
            "type": self.type,
            "turn_id": self.turn_id,
            "data": self.data,
            "timestamp": self.timestamp,
        }
        if self.meta:
            payload.update(self.meta)
        return json.dumps(payload, ensure_ascii=False)


class WSCallback:
    """Strands callback -> WS streaming.

    Mirrors websocket_ref.py behavior:
    - chunk: reasoningText (reasoning flag) + data
    - tool_start: on tool change (tool_number)
    - tool_end: on toolResult (success)

    IMPORTANT:
    - Do NOT call tools (esp. history) from inside this callback.
      That creates recursion where "history" becomes the observed tool.
    - We only STREAM here. Persistence happens AFTER the turn.
    """

    def __init__(self, websocket, loop, turn_id: str):
        self.ws = websocket
        self.loop = loop
        self.turn_id = turn_id
        self.tool_count = 0
        self.previous_tool_use = None

        # turn-local action capture (no tool calls)
        self.actions: list[dict] = []  # [{type:'tool_start'|'tool_end', ...}]

    async def _send(self, msg_type: str, data: Any = "", meta: Dict[str, Any] | None = None):
        try:
            await self.ws.send(StreamMsg(msg_type, self.turn_id, time.time(), data, meta).dumps())
        except Exception:
            pass

    def _schedule(self, msg_type: str, data: Any = "", meta: Dict[str, Any] | None = None):
        asyncio.run_coroutine_threadsafe(self._send(msg_type, data, meta), self.loop)

    def __call__(self, **kwargs: Any) -> None:
        reasoning_text = kwargs.get("reasoningText")
        data = kwargs.get("data")
        current_tool_use = kwargs.get("current_tool_use") or {}
        message = kwargs.get("message") or {}

        if reasoning_text:
            self._schedule("chunk", reasoning_text, {"reasoning": True})

        if data:
            self._schedule("chunk", data)

        if isinstance(current_tool_use, dict) and current_tool_use.get("name"):
            if self.previous_tool_use != current_tool_use:
                self.previous_tool_use = current_tool_use
                self.tool_count += 1
                tool_name = current_tool_use.get("name", "Unknown tool")

                self._schedule("tool_start", tool_name, {"tool_number": self.tool_count})

                # capture for persistence later
                self.actions.append(
                    {
                        "type": "tool_start",
                        "tool": tool_name,
                        "tool_number": self.tool_count,
                        "ts": time.time(),
                    }
                )

        # Tool results: Strands emits toolResult inside message content
        if isinstance(message, dict) and message.get("role") == "user":
            for content in message.get("content", []):
                if isinstance(content, dict):
                    tool_result = content.get("toolResult")
                    if tool_result:
                        status = tool_result.get("status", "unknown")
                        success = status == "success"

                        self._schedule("tool_end", status, {"success": success})

                        # capture for persistence later
                        self.actions.append(
                            {
                                "type": "tool_end",
                                "status": status,
                                "success": success,
                                "ts": time.time(),
                            }
                        )


async def _send_ui_error(ws, turn_id: str, err: str):
    try:
        await ws.send(StreamMsg("error", turn_id, time.time(), err).dumps())
    except Exception:
        pass


def _history_emit_and_store(agent, websocket, turn_id: str, event_type: str, data: dict):
    """Store history via history tool + emit to UI. MUST be called outside callbacks."""
    if not (hasattr(agent, "tool") and "history" in getattr(agent, "tool_names", [])):
        return

    try:
        res = agent.tool.history(
            action="add",
            event_type=event_type,
            data=data,
            turn_id=turn_id,
            record_direct_tool_call=False,
        )
        rec = res.get("record")
        if rec:
            # emit as a history event
            asyncio.create_task(websocket.send(StreamMsg("history", turn_id, time.time(), rec).dumps()))
    except Exception:
        pass


async def _handle_ui_action(agent, websocket, payload: dict, client_creds: dict):
    turn_id = payload.get("turn_id") or f"ui-{uuid.uuid4()}"
    action = payload.get("action")

    if action == "fetch_ohlcv":
        symbol = payload.get("symbol", "BTC/USDT")
        timeframe = payload.get("timeframe", "1m")
        limit = int(payload.get("limit", 240))
        exchange_id = payload.get("exchange") or client_creds.get("exchange") or os.getenv("DASH_EXCHANGE", "bybit")

        try:
            if ccxt is None:
                raise ImportError("ccxt not installed")

            exchange_class = getattr(ccxt, exchange_id)

            # fetch_ohlcv is a PUBLIC endpoint - no API keys needed
            # Adding keys can cause issues (testnet/mainnet mismatch, wrong permissions)
            cfg = {"enableRateLimit": True}

            # Exchange-specific options
            if exchange_id == "bybit":
                cfg["options"] = {"defaultType": "spot"}  # Use spot for public data
            elif exchange_id == "binance":
                cfg["options"] = {"defaultType": "spot"}
            elif exchange_id == "okx":
                cfg["options"] = {"defaultType": "spot"}

            exchange_instance = exchange_class(cfg)
            ohlcv = exchange_instance.fetch_ohlcv(symbol, timeframe, limit=limit)

            await websocket.send(
                StreamMsg(
                    "ohlcv",
                    turn_id,
                    time.time(),
                    {"symbol": symbol, "timeframe": timeframe, "ohlcv": ohlcv},
                ).dumps()
            )

            # Note: Not logging fetch_ohlcv to history - it's a routine chart refresh

        except Exception as e:
            await _send_ui_error(websocket, turn_id, f"fetch_ohlcv failed: {e}")
        return

    if action == "fetch_balance":
        exchange_id = payload.get("exchange") or client_creds.get("exchange") or os.getenv("DASH_EXCHANGE", "bybit")
        api_key = payload.get("apiKey") or client_creds.get("apiKey") or os.getenv("CCXT_API_KEY", "")
        api_secret = payload.get("apiSecret") or client_creds.get("apiSecret") or os.getenv("CCXT_SECRET", "")

        if not api_key or not api_secret:
            await websocket.send(
                StreamMsg(
                    "balance",
                    turn_id,
                    time.time(),
                    {"status": "no_credentials", "total": {"USDT": 0}, "free": {"USDT": 0}},
                ).dumps()
            )
            return

        try:
            if ccxt is None:
                raise ImportError("ccxt not installed")

            exchange_class = getattr(ccxt, exchange_id)

            # Balance is a PRIVATE endpoint - needs API keys
            cfg = {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
            }

            # Exchange-specific options
            if exchange_id == "bybit":
                cfg["options"] = {"defaultType": "spot"}
            elif exchange_id == "binance":
                cfg["options"] = {"defaultType": "spot"}
            elif exchange_id == "okx":
                cfg["options"] = {"defaultType": "spot"}

            exchange_instance = exchange_class(cfg)
            balance = exchange_instance.fetch_balance()

            # Extract relevant balance info
            total = balance.get("total", {})
            free = balance.get("free", {})

            # Filter to non-zero balances
            non_zero_total = {k: v for k, v in total.items() if v and float(v) > 0}
            non_zero_free = {k: v for k, v in free.items() if v and float(v) > 0}

            await websocket.send(
                StreamMsg(
                    "balance",
                    turn_id,
                    time.time(),
                    {"status": "success", "total": non_zero_total, "free": non_zero_free},
                ).dumps()
            )

            # Persist balance action as history
            _history_emit_and_store(
                agent,
                websocket,
                turn_id,
                "balance",
                {"exchange": exchange_id, "total": non_zero_total},
            )

        except Exception as e:
            await websocket.send(
                StreamMsg(
                    "balance",
                    turn_id,
                    time.time(),
                    {"status": "error", "error": str(e), "total": {}, "free": {}},
                ).dumps()
            )
        return

    await _send_ui_error(websocket, turn_id, f"Unknown UI action: {action}")


async def run_turn(agent, websocket, loop, user_text: str, turn_id: str):
    await websocket.send(StreamMsg("turn_start", turn_id, time.time(), user_text).dumps())

    cb = WSCallback(websocket, loop, turn_id)
    agent.callback_handler = cb

    await loop.run_in_executor(None, agent, user_text)

    # Persist captured tool actions (safe: after turn)
    for a in cb.actions:
        if a.get("type") == "tool_start":
            _history_emit_and_store(
                agent,
                websocket,
                turn_id,
                "tool_start",
                {"tool": a.get("tool"), "tool_number": a.get("tool_number")},
            )
        elif a.get("type") == "tool_end":
            _history_emit_and_store(
                agent,
                websocket,
                turn_id,
                "tool_end",
                {"status": a.get("status"), "success": bool(a.get("success"))},
            )

    # best-effort balance snapshot after each turn
    try:
        if hasattr(agent, "tool") and "balance" in getattr(agent, "tool_names", []):
            bal_res = agent.tool.balance(action="get", record_direct_tool_call=False)
            await websocket.send(StreamMsg("balance", turn_id, time.time(), bal_res).dumps())

            # Persist balance (safe: after turn)
            _history_emit_and_store(
                agent,
                websocket,
                turn_id,
                "balance",
                {"balance": bal_res},
            )

    except Exception as e:
        await websocket.send(StreamMsg("balance", turn_id, time.time(), {"status": "error", "error": str(e)}).dumps())

    await websocket.send(StreamMsg("turn_end", turn_id, time.time()).dumps())


async def handle_client(websocket):
    loop = asyncio.get_running_loop()

    # Ensure local ./tools are discoverable
    os.environ.setdefault("DEVDUCK_LOAD_TOOLS_FROM_DIR", "true")
    proj_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    os.chdir(proj_root)

    dd = DevDuck(auto_start_servers=False)
    agent = dd.agent

    # Per-client credentials (set via 'credentials' message from UI)
    client_creds = {
        "exchange": os.getenv("DASH_EXCHANGE", "bybit"),
        "apiKey": "",
        "apiSecret": ""
    }

    await websocket.send(StreamMsg("connected", "", time.time(), "connected").dumps())

    # send recent history (best-effort)
    try:
        if hasattr(agent, "tool") and "history" in getattr(agent, "tool_names", []):
            tail = agent.tool.history(action="tail", limit=200, record_direct_tool_call=False)
            items = tail.get("items") or []
            await websocket.send(StreamMsg("history_sync", "", time.time(), items).dumps())
    except Exception:
        pass

    active = set()
    async for raw in websocket:
        raw = (raw or "").strip()
        if not raw:
            continue

        if raw.startswith("{"):
            try:
                payload = json.loads(raw)

                # Handle credentials update from UI
                if isinstance(payload, dict) and payload.get("type") == "credentials":
                    client_creds["exchange"] = payload.get("exchange") or client_creds["exchange"]
                    client_creds["apiKey"] = payload.get("apiKey") or ""
                    client_creds["apiSecret"] = payload.get("apiSecret") or ""
                    await websocket.send(StreamMsg("credentials_updated", "", time.time(), {"exchange": client_creds["exchange"]}).dumps())
                    continue

                if isinstance(payload, dict) and payload.get("type") == "ui":
                    await _handle_ui_action(agent, websocket, payload, client_creds)
                    continue
                if isinstance(payload, dict) and payload.get("type") == "history" and payload.get("action") == "clear":
                    if hasattr(agent, "tool") and "history" in getattr(agent, "tool_names", []):
                        agent.tool.history(action="clear", record_direct_tool_call=False)
                        await websocket.send(StreamMsg("history_cleared", "", time.time(), "cleared").dumps())
                    continue
            except Exception:
                pass

        if raw.lower() == "exit":
            await websocket.send(StreamMsg("disconnected", "", time.time(), "bye").dumps())
            break

        turn_id = str(uuid.uuid4())
        task = asyncio.create_task(run_turn(agent, websocket, loop, raw, turn_id))
        active.add(task)
        task.add_done_callback(active.discard)

    if active:
        await asyncio.gather(*active, return_exceptions=True)


async def amain():
    host = os.getenv("DASH_HOST", "127.0.0.1")
    port = int(os.getenv("DASH_PORT", "8090"))
    async with websockets.serve(handle_client, host, port):
        print(f"WS running: ws://{host}:{port}")
        await asyncio.Future()


def main():
    asyncio.run(amain())


if __name__ == "__main__":
    main()
