#!/usr/bin/env python3
"""ccxt tools for strands-trading-dashboard.

Ported from your hashtrade-master/tools/ccxt_generic.py with minimal changes.
"""

import os
from typing import Dict, Any

import ccxt
from strands import tool


def _get_creds(exchange: str, api_key: str | None, api_secret: str | None):
    if not api_key:
        api_key = os.getenv("BYBIT_API_KEY") if exchange == "bybit" else os.getenv(f"{exchange.upper()}_API_KEY")
    if not api_secret:
        api_secret = os.getenv("BYBIT_API_SECRET") if exchange == "bybit" else os.getenv(f"{exchange.upper()}_API_SECRET")
    return api_key, api_secret


def _needs_auth(method: str) -> bool:
    if not method:
        return False
    return method.startswith((
        'create_',
        'cancel_',
        'edit_',
        'withdraw',
        'fetch_my_',
        'fetch_open_',
        'fetch_orders',
        'fetch_positions',
        'fetch_balance',
    ))



@tool
def ccxt_generic(
    exchange: str = "bybit",
    method: str | None = None,
    args: str = "[]",
    kwargs: str = "{}",
    api_key: str | None = None,
    api_secret: str | None = None,
    async_mode: bool = False,  # kept for compatibility; CCXT sync in this tool
) -> Dict[str, Any]:
    """Generic CCXT tool - call any CCXT method on any exchange."""
    import json

    try:
        args_list = json.loads(args) if isinstance(args, str) else args
        kwargs_dict = json.loads(kwargs) if isinstance(kwargs, str) else kwargs

        api_key, api_secret = _get_creds(exchange, api_key, api_secret)

        testnet = (
            os.getenv("BYBIT_TESTNET", "false").lower() == "true" if exchange == "bybit" else False
        )

        exchange_class = getattr(ccxt, exchange)

        # IMPORTANT: do NOT attach credentials for public methods.
        # This avoids failing public endpoints due to expired keys.
        cfg = {"enableRateLimit": True, "options": {"recvWindow": 10000}}
        if exchange == "bybit":
            cfg["options"].update({"defaultType": "swap", "testnet": testnet})
        if method and _needs_auth(method):
            cfg["apiKey"] = api_key
            cfg["secret"] = api_secret
        exchange_instance = exchange_class(cfg)

        if not method:
            methods = [
                m
                for m in dir(exchange_instance)
                if not m.startswith("_") and callable(getattr(exchange_instance, m))
            ]

            public_methods = [
                m
                for m in methods
                if not m.startswith("private") and "sign" not in m.lower()
            ]
            fetch_methods = [m for m in public_methods if m.startswith("fetch_")]
            create_methods = [m for m in public_methods if m.startswith("create_")]
            watch_methods = [m for m in public_methods if m.startswith("watch_")]
            other_methods = [
                m for m in public_methods if m not in fetch_methods + create_methods + watch_methods
            ]

            return {
                "status": "success",
                "content": [
                    {
                        "text": (
                            f"📚 Available CCXT methods for {exchange}:\n\n"
                            f"**Fetch Methods ({len(fetch_methods)}):**\n"
                            + "\n".join(f"  • {m}" for m in fetch_methods[:15])
                            + (f"\n  ... and {max(0, len(fetch_methods) - 15)} more\n\n")
                            + f"**Create Methods ({len(create_methods)}):**\n"
                            + "\n".join(f"  • {m}" for m in create_methods)
                            + f"\n\n**Watch Methods ({len(watch_methods)}):**\n"
                            + "\n".join(f"  • {m}" for m in watch_methods[:10])
                            + f"\n\n**Other Methods ({len(other_methods)}):**\n"
                            + "\n".join(f"  • {m}" for m in other_methods[:10])
                            + f"\n\nUsage: ccxt_generic(exchange='{exchange}', method='fetch_ticker', args='[\"BTC/USDT\"]')"
                        )
                    }
                ],
            }

        method_func = getattr(exchange_instance, method)
        result = method_func(*args_list, **kwargs_dict)

        result_str = json.dumps(result, indent=2, default=str)
        max_length = 8000
        if len(result_str) > max_length:
            result_str = result_str[:max_length] + f"\n\n... (truncated, showing first {max_length} chars of {len(result_str)})"

        return {
            "status": "success",
            "content": [{"text": f"✅ {exchange}.{method}() result:\n\n```json\n{result_str}\n```"}],
        }

    except AttributeError:
        return {
            "status": "error",
            "content": [
                {
                    "text": f"Method '{method}' not found for exchange '{exchange}'. Use ccxt_generic(exchange='{exchange}') to list available methods."
                }
            ],
        }
    except Exception as e:
        return {
            "status": "error",
            "content": [{"text": f"CCXT error: {str(e)}\n\nType: {type(e).__name__}"}],
        }


@tool
def ccxt_multi_exchange_orderbook(
    exchanges: str = '["binance", "bybit", "okx"]',
    symbol: str = "BTC/USDT",
) -> Dict[str, Any]:
    """Fetch orderbook from multiple exchanges sequentially for arbitrage analysis."""
    import json

    try:
        exchanges_list = json.loads(exchanges) if isinstance(exchanges, str) else exchanges
        results = []

        for exchange_id in exchanges_list:
            try:
                exchange_class = getattr(ccxt, exchange_id)
                ex = exchange_class({"enableRateLimit": True})
                ex.load_markets()
                ob = ex.fetch_order_book(symbol)
                results.append(
                    {
                        "exchange": exchange_id,
                        "symbol": symbol,
                        "best_bid": ob["bids"][0] if ob["bids"] else None,
                        "best_ask": ob["asks"][0] if ob["asks"] else None,
                        "bid_depth": len(ob["bids"]),
                        "ask_depth": len(ob["asks"]),
                    }
                )
            except Exception as e:
                results.append({"exchange": exchange_id, "error": str(e)})

        valid = [r for r in results if "error" not in r]
        arbitrage_info = ""
        if len(valid) >= 2:
            best_bid_ex = max(valid, key=lambda x: x["best_bid"][0] if x["best_bid"] else 0)
            best_ask_ex = min(valid, key=lambda x: x["best_ask"][0] if x["best_ask"] else float("inf"))
            spread = (
                best_bid_ex["best_bid"][0] - best_ask_ex["best_ask"][0]
                if best_bid_ex["best_bid"] and best_ask_ex["best_ask"]
                else 0
            )
            spread_pct = (
                (spread / best_ask_ex["best_ask"][0] * 100)
                if best_ask_ex["best_ask"] and best_ask_ex["best_ask"][0] > 0
                else 0
            )
            arbitrage_info = (
                "\n\n🎯 **Arbitrage Opportunity:**\n"
                f"Buy on {best_ask_ex['exchange']} @ ${best_ask_ex['best_ask'][0]:.2f}\n"
                f"Sell on {best_bid_ex['exchange']} @ ${best_bid_ex['best_bid'][0]:.2f}\n"
                f"Potential profit: ${spread:.2f} ({spread_pct:.3f}%)"
            )

        txt = "📚 Multi-Exchange Orderbook Comparison:\n\n"
        for r in results:
            if "error" in r:
                txt += f"❌ {r['exchange']}: {r['error']}\n"
            else:
                txt += f"✅ {r['exchange']}: Best bid ${r['best_bid'][0]:.2f}, Best ask ${r['best_ask'][0]:.2f}\n"
        txt += arbitrage_info

        return {"status": "success", "content": [{"text": txt}]}

    except Exception as e:
        return {"status": "error", "content": [{"text": f"Multi-exchange fetch error: {str(e)}"}]}
