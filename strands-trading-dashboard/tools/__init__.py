"""Local tools package for DevDuck auto-load.

DevDuck/Strands loads tools from ./tools directory.
Import ccxt-tool-strands tools here so they are available to the agent.
"""

# Import all tools from ccxt-tool-strands package
from ccxt_tool_strands import ccxt_generic, ccxt_multi_exchange_orderbook

# Optional: ccxt_pro_watch requires ccxtpro
try:
    from ccxt_tool_strands import ccxt_pro_watch
except ImportError:
    pass  # ccxtpro not installed
