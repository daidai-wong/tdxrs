"""ETF/基金模块测试 (独立 runner, 无 pytest 依赖)

历史说明: 旧版测试引用已移除的 TdxHqEtfClient (tdxrs.pro)。
ETF 功能 v0.6.3 起合并至标准模块 tdxrs.TdxHqFundClient,
本测试改为验证 TdxHqFundClient 的路由/分类静态方法与可选网络功能。
"""

from tdxrs import TdxHqFundClient
from tdxrs.constants import MARKET_SH, MARKET_SZ

_FAILS = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}")
        _FAILS.append(name)


def test_static_routing():
    """静态路由/分类方法 (离线)"""
    # is_fund: 基金代码 vs 股票代码
    check("is_fund(sh 510300)", TdxHqFundClient.is_fund(MARKET_SH, "510300") is True)
    check("is_fund(sz 159915)", TdxHqFundClient.is_fund(MARKET_SZ, "159915") is True)
    check("is_fund(sh 600519)=False", TdxHqFundClient.is_fund(MARKET_SH, "600519") is False)
    check("is_fund(sz 000858)=False", TdxHqFundClient.is_fund(MARKET_SZ, "000858") is False)

    # auto_market_code
    for code, mkt, name in [("510300", MARKET_SH, "ETF SH"),
                            ("600519", MARKET_SH, "stock SH"),
                            ("159915", MARKET_SZ, "ETF SZ"),
                            ("000858", MARKET_SZ, "stock SZ")]:
        got = TdxHqFundClient.auto_market_code(code)
        check(f"auto_market_code({code})=={mkt}", got == mkt, f"got {got}")

    # classify_fund: 返回非空类型名 (ETF/LOF/REITs/...)
    t = TdxHqFundClient.classify_fund(MARKET_SH, "510300")
    check("classify_fund(510300) non-empty", isinstance(t, str) and len(t) > 0, f"got {t!r}")
    print(f"    classify_fund(510300) = {t}")


def test_network_optional():
    """网络功能 (可选): 连不上则 SKIP, 不算失败"""
    try:
        c = TdxHqFundClient()
        if not c.connect_to_any(8.0):
            print("SKIP network: 无法连接行情服务器")
            return
        lst = c.get_fund_list(MARKET_SH)
        check("get_fund_list(sh) non-empty", bool(lst), f"got {len(lst or [])}")
        if lst:
            first = lst[0]
            code0 = first.get("code", "")
            print(f"    sample: {code0} {first.get('name', '')}")
        quotes = c.get_fund_quotes([(MARKET_SH, "510300"), (MARKET_SZ, "159915")])
        check("get_fund_quotes 2只", bool(quotes) and len(quotes) == 2)
        for q in quotes or []:
            print(f"    {q.get('code')}: price={q.get('price')}")
        c.disconnect()
    except Exception as e:
        print(f"SKIP network: {e}")


def main() -> int:
    print("===== TdxHqFundClient (ETF) module test =====")
    test_static_routing()
    test_network_optional()
    print(f"\nfailed={len(_FAILS)}")
    if _FAILS:
        print("failed:", _FAILS)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
