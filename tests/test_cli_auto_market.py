"""cli.auto_market 市场路由测试 (独立 runner, 无 pytest 依赖)

验证股票代码 -> 市场映射, 重点覆盖北交所路由修复:
- 修复前: 43/83/87 开头全部误判为深圳, 920 开头误判为上海
"""

from tdxrs.constants import MARKET_BJ, MARKET_SH, MARKET_SZ
from tdxrs.cli import auto_market

CASES = [
    # (code, expected_market)
    ("600519", MARKET_SH), ("601318", MARKET_SH), ("688001", MARKET_SH),
    ("510300", MARKET_SH), ("588000", MARKET_SH),
    ("900901", MARKET_SH),   # 沪市 B 股 (920xxx 是北交所, 不能被 9 开头规则吞掉)
    ("430047", MARKET_BJ), ("832566", MARKET_BJ), ("836819", MARKET_BJ),
    ("871981", MARKET_BJ), ("920002", MARKET_BJ), ("920108", MARKET_BJ),
    ("000001", MARKET_SZ), ("000858", MARKET_SZ),
    ("300750", MARKET_SZ), ("301236", MARKET_SZ),
    ("159915", MARKET_SZ), ("127012", MARKET_SZ),
]


def main() -> int:
    failed = []
    for code, expected in CASES:
        got = auto_market(code)
        if got == expected:
            print(f"PASS auto_market({code}) == {expected}")
        else:
            print(f"FAIL auto_market({code}) = {got}, expected {expected}")
            failed.append(code)
    print(f"\n{len(CASES) - len(failed)}/{len(CASES)} pass")
    if failed:
        print("failed:", failed)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
