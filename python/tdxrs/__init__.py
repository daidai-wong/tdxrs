"""tdxrs - 通达信行情数据解析库 (Rust 实现)

核心模块:
- Reader: 日线、分钟线、板块、财务数据解析
- Client: 行情客户端 (TdxHqClient, AsyncTdxHqClient, TdxDirectClient, TdxSmartClient, TdxHqFundClient, TdxBlockClient)

用法:
    from tdxrs import TdxHqClient, TdxSmartClient, DailyBarReader
"""

try:
    from tdxrs._internal import (
        DailyBarReader, MinBarReader, LcMinBarReader, BlockReader, FinancialReader,
        TdxHqClient, AsyncTdxHqClient, TdxDirectClient, TdxSmartClient, TdxHqFundClient, TdxBlockClient,
        PRIMARY_SERVERS, ALL_KNOWN_SERVERS,
    )
except ImportError:
    raise ImportError(
        "tdxrs native module not found. Please install with: pip install tdxrs"
    )

# 服务器健康筛查 (纯 Python 层, 复用 Rust probe_servers)
from tdxrs.server_health import screen_servers, best_server

# 混合数据层 (本地 vipdoc 优先 + 服务器补缺 + 双源验证)
from tdxrs.hybrid import HybridClient, get_daily_bars

__version__ = "0.6.7"
__all__ = [
    "DailyBarReader", "MinBarReader", "LcMinBarReader", "BlockReader", "FinancialReader",
    "TdxHqClient", "AsyncTdxHqClient", "TdxDirectClient", "TdxSmartClient", "TdxHqFundClient", "TdxBlockClient",
    "PRIMARY_SERVERS", "ALL_KNOWN_SERVERS",
    "screen_servers", "best_server",
    "HybridClient", "get_daily_bars",
]
