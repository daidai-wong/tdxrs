"""网络 API 性能基准

测试 tdxrs 网络客户端的 API 性能 (TdxHqClient 连接池模式)。

用法:
  python tests/bench_network.py                                # 全量
  python tests/bench_network.py --method kline                  # 仅 K 线
  python tests/bench_network.py --rounds 20 --warmup 3
  python tests/bench_network.py --json report.json --md report.md
"""

import argparse
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
from tests.bench_utils import (
    markdown_report, json_report, init_rust_import
)

init_rust_import()
from tdxrs import TdxHqClient
from tdxrs.constants import (
    MARKET_SH, MARKET_SZ,
    KLINE_DAILY, KLINE_5MIN,
    FQ_QFQ, FQ_NONE,
)


def run_api(name: str, func, rounds: int, warmup: int) -> BenchmarkResult:
    """运行单个 API benchmark"""
    # Warmup
    for _ in range(warmup):
        try:
            func()
        except Exception:
            pass

    samples = []
    records = 0
    for _ in range(rounds):
        t0 = time.perf_counter()
        result = func()
        elapsed = (time.perf_counter() - t0) * 1000
        samples.append(elapsed)
        if isinstance(result, (list, tuple)):
            records = len(result)
        elif isinstance(result, int):
            records = result

    return BenchmarkResult(
        library="tdxrs",
        method=f"api_{name}",
        label=name,
        times_ms=samples,
        record_count=records,
    )


def main():
    parser = argparse.ArgumentParser(description="tdxrs Network API benchmarks")
    parser.add_argument("--method", choices=["kline", "quotes", "info", "xdxr", "all"], default="all")
    parser.add_argument("--rounds", type=int, default=15)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--json", type=str, help="JSON output path")
    parser.add_argument("--md", type=str, help="Markdown output path")
    args = parser.parse_args()

    print("Connecting to TDX server...")
    client = TdxHqClient()
    client.connect_to_any(timeout=5.0)
    client.set_auto_retry(False)
    print(f"  Connected: {client.is_connected()}")

    suite = Suite(config={
        "rounds": args.rounds,
        "warmup": args.warmup,
        "client": "TdxHqClient (connection pool)",
    })

    tests = [
        # (name, method filter, func)
        ("kline_daily_100", lambda: client.get_security_bars(KLINE_DAILY, MARKET_SH, "600519", 0, 100, FQ_QFQ)),
        ("kline_daily_800", lambda: client.get_security_bars(KLINE_DAILY, MARKET_SH, "600519", 0, 800, FQ_QFQ)),
        ("kline_5min_200", lambda: client.get_security_bars(KLINE_5MIN, MARKET_SH, "600519", 0, 200, FQ_NONE)),
        ("quotes_3", lambda: client.get_security_quotes([
            (MARKET_SH, "600519"), (MARKET_SZ, "000858"), (MARKET_SZ, "300750")
        ])),
        ("sec_count", lambda: client.get_security_count(MARKET_SH)),
        ("sec_list", lambda: client.get_security_list(MARKET_SH, 0)),
        ("xdxr", lambda: client.get_xdxr_info(MARKET_SH, "600519")),
        ("finance", lambda: client.get_finance_info(MARKET_SH, "600519")),
    ]

    for name, func in tests:
        if args.method != "all" and not name.startswith(args.method):
            continue
        try:
            result = run_api(name, func, args.rounds, args.warmup)
            suite.add(result)
            print(f"  {name:22s}  mean={fmt_ms(result.mean_ms):>7s}ms  "
                  f"p95={fmt_ms(result.p95_ms):>7s}ms  records={result.record_count}")
        except Exception as e:
            suite.add(BenchmarkResult("tdxrs", f"api_{name}", name, [], error=str(e)))
            print(f"  {name:22s}  ERROR: {e}")

    client.disconnect()

    # Reports
    if args.md:
        markdown_report(suite, args.md)
    if args.json:
        json_report(suite, args.json)

    # Summary
    print(f"\n=== Summary ({suite.timestamp}) ===")
    for r in suite.results:
        if not r.error and not r.skipped:
            print(f"  {r.label:22s} {fmt_ms(r.mean_ms):>7s}ms")


if __name__ == "__main__":
    main()
