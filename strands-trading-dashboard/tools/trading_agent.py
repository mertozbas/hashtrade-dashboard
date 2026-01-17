#!/usr/bin/env python3
"""Minimal trading agent tool.

Purpose:
- Uses ccxt_generic for balance + create_order
- Logs trade events into history tool

This keeps your system "end-to-end": UI -> agent -> ccxt -> history -> UI panels.

NOTE: This is a demo; do not use live keys unless you know what you're doing.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict

from strands import tool


def _parse_buy_message(text: str) -> dict | None:
    """Parse: '5 dollars eth' / '10 usdt btc' etc.

    Returns: {quote_amount: float, base: 'ETH', quote: 'USDT'}
    """
    t = (text or "").lower().strip()
    # simple patterns
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:\$|usd|usdt|dollars?)\s*([a-z]{2,10})", t)
    if not m:
        return None
    amt = float(m.group(1))
    base = m.group(2).upper()
    quote = "USDT"
    return {"quote_amount": amt, "base": base, "quote": quote}


@tool
def trading_agent_turn(
    text: str,
    exchange: str = "bybit",
    market_type: str = "swap",
) -> Dict[str, Any]:
    """Single decision + action turn.

    - Always fetch balance via ccxt_generic (public if possible, private needs keys)
    - If user asks like '5 dollars eth', place market buy for that quote notional
    - Log into history tool as trade event (create_order)

    Requires:
    - ccxt_generic tool (tools/ccxt_tool.py)
    - history tool (tools/history.py)
    """

    # Late imports so tool loads even if others missing
    from devduck import devduck

    agent = devduck.agent

    # 1) balance via ccxt
    bal = None
    try:
        bal = agent.tool.ccxt_generic(
            exchange=exchange,
            method="fetch_balance",
            args=json.dumps([]),
            kwargs=json.dumps({}),
            record_direct_tool_call=False,
        )
    except Exception as e:
        bal = {"status": "error", "error": str(e)}

    # 2) intent
    intent = _parse_buy_message(text)
    if not intent:
        return {
            "status": "success",
            "action": "none",
            "balance": bal,
            "content": [{"text": "No trade intent parsed. Try: '5 dollars eth'"}],
        }

    symbol = f"{intent['base']}/{intent['quote']}"
    quote_amount = intent["quote_amount"]

    # 3) get price to convert quote -> base amount
    ticker = agent.tool.ccxt_generic(
        exchange=exchange,
        method="fetch_ticker",
        args=json.dumps([symbol]),
        record_direct_tool_call=False,
    )
    # extract last from returned JSON block
    txt = ticker.get("content", [{}])[0].get("text", "")
    last = None
    try:
        if "```json" in txt:
            j = json.loads(txt.split("```json", 1)[1].split("```", 1)[0])
            last = float(j.get("last") or j.get("close") or 0)
    except Exception:
        last = None
    if not last or last <= 0:
        return {"status": "error", "content": [{"text": f"Could not fetch price for {symbol}"}], "balance": bal}

    amount_base = quote_amount / last

    # 4) place order
    order_res = agent.tool.ccxt_generic(
        exchange=exchange,
        method="create_order",
        args=json.dumps([symbol, "market", "buy", amount_base]),
        kwargs=json.dumps({"type": "market"}),
        record_direct_tool_call=False,
    )

    # 5) history log trade marker
    try:
        agent.tool.history(
            action="add",
            event_type="trade",
            data={
                "exchange": exchange,
                "symbol": symbol,
                "side": "buy",
                "quote_amount": quote_amount,
                "amount": amount_base,
                "price_est": last,
                "order": order_res,
                "ts": time.time(),
            },
            turn_id="",
            record_direct_tool_call=False,
        )
    except Exception:
        pass

    return {
        "status": "success",
        "action": "create_order",
        "symbol": symbol,
        "side": "buy",
        "quote_amount": quote_amount,
        "amount": amount_base,
        "price_est": last,
        "balance": bal,
        "order": order_res,
        "content": [{"text": f"Placed market BUY {amount_base:.6f} {intent['base']} (~{quote_amount} {intent['quote']})."}],
    }
