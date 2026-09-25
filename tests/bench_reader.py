"""本地 Reader 性能基准

测试 tdxrs 四种本地文件解析器的吞吐量。

用法:
  python tests/bench_reader.py                     # 全量
  python tests/bench_reader.py --method daily       # 仅日线
  python tests/bench_reader.py --rounds 50 --warmup 10
"""

import argparse
import sys

# — path setup —
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
from tests.bench_utils import (
    BenchmarkResult, Suite, fmt_ms, markdown_report, json_report,
    FIXTURE_DIR, init_rust_import
)

# — import modules —
init_rust_import()
import tdxrs


def run_reader(name: str, reader_cls, fixture: str, rounds: int, warmup: int, *, coefficient: bool = False) -> BenchmarkResult:
    """运行单个 reader benchmark"""
    data = (FIXTURE_DIR / fixture).read_bytes()
    if coefficient:
        reader = reader_cls(coefficient=0.01)
    else:
        reader = reader_cls()

    # Pick fastest available parse method
    if hasattr(reader, 'parse_data_tuples'):
        parse_fn = lambda d: reader.parse_data_tuples(d)
    else:
        parse_fn = lambda d: reader.parse_data(d)

    # Warmup
    for _ in range(warmup):
        parse_fn(data)

    # Timed
    import time
    samples = []
    records = 0
    for _ in range(rounds):
        t0 = time.perf_counter()
        result = parse_fn(data)
        elapsed = (time.perf_counter() - t0) * 1000
        samples.append(elapsed)
        records = len(result)

    return BenchmarkResult(
        library="tdxrs",
        method=f"reader_{name}",
        label=f"{name} ({fixture})",
        times_ms=samples,
        record_count=records,
    )


def main():
    parser = argparse.ArgumentParser(description="tdxrs Reader benchmarks")
    parser.add_argument("--method", choices=["daily", "min", "lc_min", "block", "financial", "all"], default="all")
    parser.add_argument("--rounds", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--json", type=str, help="JSON output path")
    parser.add_argument("--md", type=str, help="Markdown output path")
    args = parser.parse_args()

    suite = Suite(config={
        "rounds": args.rounds,
        "warmup": args.warmup,
        "fixtures_dir": str(FIXTURE_DIR),
    })

    cases = [
        ("daily", tdxrs.DailyBarReader, "600519.day", True),
        ("min", tdxrs.MinBarReader, "600519.lc5", False),
        ("lc_min", tdxrs.LcMinBarReader, "600519.lc5", False),
        ("block", tdxrs.BlockReader, "test_block.dat", False),
        ("financial", tdxrs.FinancialReader, "test_finance.dat", False),
    ]

    for name, cls, fixture, has_coeff in cases:
        if args.method != "all" and name != args.method:
            continue
        try:
            result = run_reader(name, cls, fixture, args.rounds, args.warmup, coefficient=has_coeff)
            suite.add(result)
            print(f"  {name:12s}  mean={fmt_ms(result.mean_ms):>7s}ms  "
                  f"p95={fmt_ms(result.p95_ms):>7s}ms  records={result.record_count}")
        except FileNotFoundError:
            suite.add(BenchmarkResult("tdxrs", f"reader_{name}", f"{name} (missing fixture)",
                                       [], skipped=True))
            print(f"  {name:12s}  SKIPPED (fixture not found)")
        except Exception as e:
            suite.add(BenchmarkResult("tdxrs", f"reader_{name}", name, [], error=str(e)))
            print(f"  {name:12s}  ERROR: {e}")

    # Reports
    if args.md:
        markdown_report(suite, args.md)
    if args.json:
        json_report(suite, args.json)

    # Default: print summary
    print(f"\n=== Summary ({suite.timestamp}) ===")
    for r in suite.results:
        if not r.skipped and not r.error:
            throughput = r.record_count / (r.mean_ms / 1000) if r.mean_ms > 0 else 0
            print(f"  {r.label:30s} {fmt_ms(r.mean_ms):>7s}ms  (~{throughput:.0f} rec/s)")


if __name__ == "__main__":
    main()
