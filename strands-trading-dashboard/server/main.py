import asyncio, json, time, uuid, os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import websockets

from devduck import DevDuck


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
    """

    def __init__(self, websocket, loop, turn_id: str):
        self.ws = websocket
        self.loop = loop
        self.turn_id = turn_id
        self.tool_count = 0
        self.previous_tool_use = None

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
                self._schedule(
                    "tool_start",
                    current_tool_use.get("name", "Unknown tool"),
                    {"tool_number": self.tool_count},
                )

        # Tool results: Strands emits toolResult inside message content
        if isinstance(message, dict) and message.get("role") == "user":
            for content in message.get("content", []):
                if isinstance(content, dict):
                    tool_result = content.get("toolResult")
                    if tool_result:
                        status = tool_result.get("status", "unknown")
                        self._schedule(
                            "tool_end",
                            status,
                            {"success": status == "success"},
                        )


async def _send_ui_error(ws, turn_id: str, err: str):
    try:
        await ws.send(StreamMsg("error", turn_id, time.time(), err).dumps())
    except Exception:
        pass


def _extract_json_block(text: str) -> Any:
    """Extract first ```json ... ``` block. Return parsed JSON or None."""
    if not text:
        return None
    try:
        if "```json" in text:
            block = text.split("```json", 1)[1].split("```", 1)[0]
            return json.loads(block)
    except Exception:
        return None
    return None


async def _handle_ui_action(agent, websocket, payload: dict):
    # Keep UI messages in the same message format
    turn_id = payload.get("turn_id") or f"ui-{uuid.uuid4()}"
    action = payload.get("action")

    if action == "fetch_ohlcv":
        symbol = payload.get("symbol", "BTC/USDT")
        timeframe = payload.get("timeframe", "1m")
        limit = int(payload.get("limit", 240))
        exchange = payload.get("exchange", os.getenv("DASH_EXCHANGE", "bybit"))

        try:
            res = agent.tool.ccxt_generic(
                exchange=exchange,
                method="fetch_ohlcv",
                args=json.dumps([symbol, timeframe, None, limit]),
                record_direct_tool_call=False,
            )
            txt = res.get("content", [{}])[0].get("text", "")
            parsed = _extract_json_block(txt)
            ohlcv = parsed if isinstance(parsed, list) else []

            await websocket.send(
                StreamMsg(
                    "ohlcv",
                    turn_id,
                    time.time(),
                    {"symbol": symbol, "timeframe": timeframe, "ohlcv": ohlcv},
                ).dumps()
            )
        except Exception as e:
            await _send_ui_error(websocket, turn_id, f"fetch_ohlcv failed: {e}")
        return

    await _send_ui_error(websocket, turn_id, f"Unknown UI action: {action}")


async def run_turn(agent, websocket, loop, user_text: str, turn_id: str):
    # IMPORTANT: frontend expects turn_start BEFORE chunks
    await websocket.send(StreamMsg("turn_start", turn_id, time.time(), user_text).dumps())

    agent.callback_handler = WSCallback(websocket, loop, turn_id)

    await loop.run_in_executor(None, agent, user_text)

    # best-effort balance snapshot after each turn
    try:
        if hasattr(agent, "tool") and "balance" in getattr(agent, "tool_names", []):
            bal_res = agent.tool.balance(action="get", record_direct_tool_call=False)
            await websocket.send(StreamMsg("balance", turn_id, time.time(), bal_res).dumps())
    except Exception as e:
        await websocket.send(
            StreamMsg("balance", turn_id, time.time(), {"status": "error", "error": str(e)}).dumps()
        )

    await websocket.send(StreamMsg("turn_end", turn_id, time.time()).dumps())


async def handle_client(websocket):
    loop = asyncio.get_running_loop()

    # Ensure local ./tools are discoverable
    os.environ.setdefault("DEVDUCK_LOAD_TOOLS_FROM_DIR", "true")
    proj_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    os.chdir(proj_root)

    # Per-connection DevDuck (like websocket_ref)
    dd = DevDuck(auto_start_servers=False)
    agent = dd.agent

    await websocket.send(StreamMsg("connected", "", time.time(), "connected").dumps())

    active = set()
    async for raw in websocket:
        raw = (raw or "").strip()
        if not raw:
            continue

        # UI control messages are JSON
        if raw.startswith("{"):
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict) and payload.get("type") == "ui":
                    await _handle_ui_action(agent, websocket, payload)
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
