"""
tdxrs Optimization Benchmark: dict vs tuple, with_capacity vs baseline
=====================================================================

Compares:
  1. dict mode (original) vs tuple mode (new optimized path)
  2. tdxrs tuple vs tdxpy (baseline)
  3. Local file reader: dict vs tuple vs tdxpy

Usage:
  python tests/bench_optimization.py [--mode reader|network|all] [--rounds 20]
"""

import argparse
import os
import sys
import time
from dataclasses import dataclass, field

# Ensure both tdxrs (local) and tdxpy (parent dir) are on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import tdxrs
from tdxpy.hq import TdxHq_API
from tdxpy.reader import (
    TdxDailyBarReader,
    TdxMinBarReader,
    TdxLCMinBarReader,
)

TDXRS_SERVER = "218.75.126.9"
TDXRS_PORT = 7709


@dataclass
class OptResult:
    label: str
    mode: str  # "dict" | "tuple" | "tdxpy"
    times_ms: list = field(default_factory=list)
    error: str | None = None

    @property
    def mean(self):
        return sum(self.times_ms) / len(self.times_ms) if self.times_ms else 0

    @property
    def min_ms(self):
        return min(self.times_ms) if self.times_ms else 0

    @property
    def max_ms(self):
        return max(self.times_ms) if self.times_ms else 0

    @property
    def std_ms(self):
        if len(self.times_ms) < 2:
            return 0
        m = self.mean
        return (sum((x - m) ** 2 for x in self.times_ms) / (len(self.times_ms) - 1)) ** 0.5


def connect_rs():
    c = tdxrs.TdxHqClient()
    c.set_cache_ttl(0)
    c.connect(TDXRS_SERVER, TDXRS_PORT)
    return c


def connect_py():
    c = TdxHq_API()
    c.connect(TDXRS_SERVER, TDXRS_PORT, time_out=5.0)
    return c


def bench(func, rounds, warmup=3):
    """Run func for warmup+rounds, return list of ms for measurement rounds."""
    for _ in range(warmup):
        try:
            func()
        except Exception:
            pass
    times = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        try:
            func()
        except Exception as e:
            return None, str(e)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000)
    return times, None


# ============================================================
# Part 1: Local Reader Benchmarks (dict vs tuple vs tdxpy)
# ============================================================

def bench_local_daily(reader_dir, stocks, rounds):
    """Benchmark DailyBarReader: dict vs tuple vs tdxpy."""
    results = []
    rs_reader = tdxrs.DailyBarReader()
    py_reader = TdxDailyBarReader()

    for stock in stocks:
        path = os.path.join(reader_dir, f"{stock}.day")
        if not os.path.exists(path):
            continue

        # tdxrs dict mode
        r = OptResult(label=f"{stock} .day dict", mode="dict")
        r.times_ms, r.error = bench(lambda: rs_reader.parse_file(path), rounds)
        results.append(r)

        # tdxrs tuple mode
        r = OptResult(label=f"{stock} .day tuple", mode="tuple")
        r.times_ms, r.error = bench(lambda: rs_reader.parse_file_tuples(path), rounds)
        results.append(r)

        # tdxpy (returns generator of tuples)
        r = OptResult(label=f"{stock} .day tdxpy", mode="tdxpy")
        r.times_ms, r.error = bench(lambda: list(py_reader.parse_data_by_file(path)), rounds)
        results.append(r)

    return results


def bench_local_minbar(reader_dir, stocks, rounds):
    """Benchmark MinBarReader (.lc5): dict vs tuple vs tdxpy."""
    results = []
    rs_reader = tdxrs.MinBarReader()
    py_reader = TdxMinBarReader()

    for stock in stocks:
        path = os.path.join(reader_dir, f"{stock}.lc5")
        if not os.path.exists(path):
            continue

        r = OptResult(label=f"{stock} .lc5 dict", mode="dict")
        r.times_ms, r.error = bench(lambda: rs_reader.parse_file(path), rounds)
        results.append(r)

        r = OptResult(label=f"{stock} .lc5 tuple", mode="tuple")
        r.times_ms, r.error = bench(lambda: rs_reader.parse_file_tuples(path), rounds)
        results.append(r)

        r = OptResult(label=f"{stock} .lc5 tdxpy", mode="tdxpy")
        r.times_ms, r.error = bench(lambda: list(py_reader.parse_data_by_file(path)), rounds)
        results.append(r)

    return results


def bench_local_lcmminbar(reader_dir, stocks, rounds):
    """Benchmark LcMinBarReader (.lc1): dict vs tuple vs tdxpy."""
    results = []
    rs_reader = tdxrs.LcMinBarReader()
    py_reader = TdxLCMinBarReader()

    for stock in stocks:
        path = os.path.join(reader_dir, f"{stock}_1min.lc1")
        if not os.path.exists(path):
            continue

        r = OptResult(label=f"{stock} .lc1 dict", mode="dict")
        r.times_ms, r.error = bench(lambda: rs_reader.parse_file(path), rounds)
        results.append(r)

        r = OptResult(label=f"{stock} .lc1 tuple", mode="tuple")
        r.times_ms, r.error = bench(lambda: rs_reader.parse_file_tuples(path), rounds)
        results.append(r)

        r = OptResult(label=f"{stock} .lc1 tdxpy", mode="tdxpy")
        r.times_ms, r.error = bench(lambda: list(py_reader.parse_data_by_file(path)), rounds)
        results.append(r)

    return results


# ============================================================
# Part 2: Network API Benchmarks (dict vs tuple)
# ============================================================

def bench_network(rs_client, py_client, rounds):
    """Benchmark network APIs: tdxrs dict vs tdxrs tuple."""
    results = []

    test_cases = [
        ("get_security_bars", "SH 600519 daily 800",
         lambda rs: rs.get_security_bars(9, 1, "600519", 0, 800),
         lambda rs: rs.get_security_bars_tuples(9, 1, "600519", 0, 800)),
        ("get_security_bars", "SH 600519 5min 800",
         lambda rs: rs.get_security_bars(0, 1, "600519", 0, 800),
         lambda rs: rs.get_security_bars_tuples(0, 1, "600519", 0, 800)),
        ("get_index_bars", "SH 000001 daily 800",
         lambda rs: rs.get_index_bars(9, 1, "000001", 0, 800),
         lambda rs: rs.get_index_bars_tuples(9, 1, "000001", 0, 800)),
        ("get_index_bars", "SZ 399001 daily 800",
         lambda rs: rs.get_index_bars(9, 0, "399001", 0, 800),
         lambda rs: rs.get_index_bars_tuples(9, 0, "399001", 0, 800)),
        ("get_security_quotes", "3 stocks",
         lambda rs: rs.get_security_quotes([(1, "600519"), (0, "000858"), (0, "300750")]),
         lambda rs: rs.get_security_quotes_tuples([(1, "600519"), (0, "000858"), (0, "300750")])),
    ]

    for name, label, dict_fn, tuple_fn in test_cases:
        # tdxrs dict
        r = OptResult(label=f"{name} {label} dict", mode="dict")
        r.times_ms, r.error = bench(lambda: dict_fn(rs_client), rounds)
        results.append(r)

        # tdxrs tuple
        r = OptResult(label=f"{name} {label} tuple", mode="tuple")
        r.times_ms, r.error = bench(lambda: tuple_fn(rs_client), rounds)
        results.append(r)

    return results


# ============================================================
# Report Generation
# ============================================================

def print_results(results, title):
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")
    print(f"{'Label':<45} {'Mode':<8} {'Mean':>8} {'Min':>8} {'Max':>8} {'Std':>8}")
    print(f"{'-' * 45} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 8}")

    for r in results:
        if r.error:
            print(f"{r.label:<45} {r.mode:<8} {'ERROR':>8}  {r.error[:40]}")
        elif r.times_ms:
            print(f"{r.label:<45} {r.mode:<8} {r.mean:>7.2f}ms {r.min_ms:>7.2f}ms {r.max_ms:>7.2f}ms {r.std_ms:>7.2f}ms")
        else:
            print(f"{r.label:<45} {r.mode:<8} {'N/A':>8}")


def print_speedup_table(results):
    """Print dict vs tuple and tuple vs tdxpy speedup ratios."""
    print(f"\n{'=' * 70}")
    print(f"  Speedup Analysis")
    print(f"{'=' * 70}")

    # Group by label prefix
    groups = {}
    for r in results:
        # Extract base label (remove mode suffix)
        base = r.label.rsplit(" ", 1)[0]
        if base not in groups:
            groups[base] = {}
        groups[base][r.mode] = r

    print(f"{'Base Label':<40} {'dict→tuple':>12} {'tuple→py':>12}")
    print(f"{'-' * 40} {'-' * 12} {'-' * 12}")

    for base, modes in groups.items():
        dict_r = modes.get("dict")
        tuple_r = modes.get("tuple")
        py_r = modes.get("tdxpy")

        dt_speedup = ""
        tp_speedup = ""

        if dict_r and tuple_r and dict_r.mean > 0 and tuple_r.mean > 0:
            ratio = dict_r.mean / tuple_r.mean
            dt_speedup = f"{ratio:.2f}x"

        if tuple_r and py_r and tuple_r.mean > 0 and py_r.mean > 0:
            ratio = py_r.mean / tuple_r.mean
            tp_speedup = f"{ratio:.2f}x"

        if dt_speedup or tp_speedup:
            print(f"{base:<40} {dt_speedup:>12} {tp_speedup:>12}")


def generate_report(all_results, output_path):
    """Generate markdown report."""
    lines = [
        "# tdxrs Optimization Benchmark Report",
        "",
        f"**Generated**: {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "---",
        "",
        "## Summary",
        "",
        "Compares tdxrs dict mode (original) vs tuple mode (optimized) vs tdxpy baseline.",
        "",
    ]

    # Group results
    local_results = [r for r in all_results if "network" not in r.label.lower()]
    network_results = [r for r in all_results if "network" in r.label.lower() or "get_" in r.label]

    if local_results:
        lines.extend([
            "### Local Reader Performance",
            "",
            "| Label | Mode | Mean (ms) | Min (ms) | Max (ms) | Speedup vs tdxpy |",
            "|-------|------|-----------|----------|----------|------------------|",
        ])
        groups = {}
        for r in local_results:
            base = r.label.rsplit(" ", 1)[0]
            if base not in groups:
                groups[base] = {}
            groups[base][r.mode] = r

        for base, modes in groups.items():
            py_r = modes.get("tdxpy")
            for mode_name in ["dict", "tuple"]:
                r = modes.get(mode_name)
                if r and r.times_ms:
                    speedup = f"{py_r.mean / r.mean:.2f}x" if py_r and py_r.mean > 0 and r.mean > 0 else "N/A"
                    lines.append(f"| {r.label} | {r.mode} | {r.mean:.2f} | {r.min_ms:.2f} | {r.max_ms:.2f} | {speedup} |")
        lines.append("")

    if network_results:
        lines.extend([
            "### Network API Performance",
            "",
            "| Label | Mode | Mean (ms) | Min (ms) | Max (ms) | dict→tuple |",
            "|-------|------|-----------|----------|----------|------------|",
        ])
        groups = {}
        for r in network_results:
            base = r.label.rsplit(" ", 1)[0]
            if base not in groups:
                groups[base] = {}
            groups[base][r.mode] = r

        for base, modes in groups.items():
            dict_r = modes.get("dict")
            for mode_name in ["dict", "tuple"]:
                r = modes.get(mode_name)
                if r and r.times_ms:
                    dt = f"{dict_r.mean / r.mean:.2f}x" if mode_name == "tuple" and dict_r and dict_r.mean > 0 and r.mean > 0 else "-"
                    lines.append(f"| {r.label} | {r.mode} | {r.mean:.2f} | {r.min_ms:.2f} | {r.max_ms:.2f} | {dt} |")
        lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\nReport saved to: {output_path}")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="tdxrs Optimization Benchmark")
    parser.add_argument("--mode", choices=["reader", "network", "all"], default="all")
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--reader-dir", default=None, help="Directory with .day/.lc5/.lc1 files")
    args = parser.parse_args()

    # Find reader dir
    reader_dir = args.reader_dir
    if reader_dir is None:
        for candidate in [
            os.path.join(os.path.dirname(__file__), "fixtures"),
            os.path.join(os.path.dirname(__file__), "..", "data"),
            os.path.join(os.path.dirname(__file__), "..", "test_data"),
            "E:/stock/tdx/new_tdx_test/T0002/hq_cache",
        ]:
            if os.path.isdir(candidate):
                reader_dir = candidate
                break
    if reader_dir is None:
        print("ERROR: No reader directory found. Use --reader-dir to specify.")
        sys.exit(1)
    print(f"Reader dir: {reader_dir}")

    stocks = ["600519", "000858", "300750", "000001"]
    all_results = []

    if args.mode in ("reader", "all"):
        print("\n--- Local Reader Benchmarks ---")
        r = bench_local_daily(reader_dir, stocks, args.rounds)
        all_results.extend(r)
        print_results(r, "DailyBarReader: dict vs tuple vs tdxpy")

        r = bench_local_minbar(reader_dir, stocks, args.rounds)
        all_results.extend(r)
        print_results(r, "MinBarReader (.lc5): dict vs tuple vs tdxpy")

        r = bench_local_lcmminbar(reader_dir, stocks, args.rounds)
        all_results.extend(r)
        print_results(r, "LcMinBarReader (.lc1): dict vs tuple vs tdxpy")

    if args.mode in ("network", "all"):
        print("\n--- Network API Benchmarks ---")
        print("Connecting...")
        rs_client = connect_rs()
        py_client = connect_py()

        r = bench_network(rs_client, py_client, args.rounds)
        all_results.extend(r)
        print_results(r, "Network API: dict vs tuple")

        rs_client.disconnect()
        py_client.disconnect()

    if all_results:
        print_speedup_table(all_results)
        report_path = os.path.join(os.path.dirname(__file__), "..", "docs", "OPTIMIZATION_REPORT.md")
        generate_report(all_results, report_path)

    print("\nDone.")


if __name__ == "__main__":
    main()
