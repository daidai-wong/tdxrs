#!/usr/bin/env python3
"""F10 模块测试 (独立 runner, 无 pytest 依赖)

TdxF10Client 需源码编译启用 f10 feature:
    maturin develop --release --features f10
未启用时本测试 SKIP (exit 0), 不算失败。
"""


FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}")
        FAILS.append(name)


def main() -> int:
    print("===== TdxF10Client module test =====")

    # ---------- feature 门控 ----------
    try:
        from tdxrs._internal import TdxF10Client
    except ImportError as e:
        print(f"SKIP: f10 feature 未编译 ({e})")
        print("启用: maturin develop --release --features f10")
        return 0

    # ---------- 静态方法 (离线) ----------
    check("is_valid_code(600519)", TdxF10Client.is_valid_code("600519") is True)
    check("is_valid_code(000858)", TdxF10Client.is_valid_code("000858") is True)
    check("is_valid_code(abc)=False", TdxF10Client.is_valid_code("abc") is False)
    check("is_valid_code(12345)=False", TdxF10Client.is_valid_code("12345") is False)
    check("auto_market_code(600519)==1", TdxF10Client.auto_market_code("600519") == 1)
    check("auto_market_code(000858)==0", TdxF10Client.auto_market_code("000858") == 0)
    check("auto_market_code(300750)==0", TdxF10Client.auto_market_code("300750") == 0)

    # ---------- 网络功能 (可选) ----------
    try:
        client = TdxF10Client("117.34.114.14", 7709)
    except Exception as e:
        print(f"SKIP network: 客户端创建失败 ({e})")
        client = None

    if client is not None:
        try:
            cats = client.get_category(1, "600519")
            check("get_category non-empty", bool(cats), f"got {len(cats or [])}")
            if cats:
                first = cats[0]
                content = client.get_content(1, "600519", first)
                check("get_content non-empty", bool(content),
                      f"got {len(content or '')} chars")
                if content:
                    print(f"    '{first.get('name')}' -> {len(content)} chars")
                parsed = TdxF10Client.parse_f10(content)
                check("parse_f10 returns dict", isinstance(parsed, dict))
        except Exception as e:
            print(f"SKIP network: {e}")

    print(f"\nfailed={len(FAILS)}")
    if FAILS:
        print("failed:", FAILS)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
