"""
tdxrs vs tdxpy Performance Benchmark

Head-to-head comparison of parsing and network API performance.

Usage:
  python tests/bench_performance.py --mode all       # Full benchmark
  python tests/bench_performance.py --mode local     # Local file readers only
  python tests/bench_performance.py --mode network   # Network API only
  python tests/bench_performance.py --rounds 30 --warmup 5
"""

import argparse
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ============================================================
# Path setup
# ============================================================

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
TDXPY_DIR = PROJECT_DIR.parent / "tdxpy"
FIXTURE_DIR = SCRIPT_DIR / "fixtures"
REPORT_DIR = PROJECT_DIR / "docs"

# ============================================================
# Data structures
# ============================================================


@dataclass
class BenchmarkResult:
    library: str
    method: str
    label: str
    times_ms: list
    record_count: int = 0
    error: Optional[str] = None
    skipped: bool = False

    @property
    def mean_ms(self):
        return statistics.mean(self.times_ms) if self.times_ms else 0

    @property
    def min_ms(self):
        return min(self.times_ms) if self.times_ms else 0

    @property
    def max_ms(self):
        return max(self.times_ms) if self.times_ms else 0

    @property
    def std_ms(self):
        return statistics.stdev(self.times_ms) if len(self.times_ms) > 1 else 0

    @property
    def p95_ms(self):
        if not self.times_ms:
            return 0
        s = sorted(self.times_ms)
        idx = int(len(s) * 0.95)
        return s[min(idx, len(s) - 1)]


@dataclass
class BenchmarkSuite:
    results: list = field(default_factory=list)
    timestamp: str = ""
    config: dict = field(default_factory=dict)

    def add(self, result):
        self.results.append(result)

    def get_pairs(self, method):
        rs = [r for r in self.results if r.library == "tdxrs" and r.method == method]
        py = [r for r in self.results if r.library == "tdxpy" and r.method == method]
        pairs = []
        for r in rs:
            for p in py:
                if r.label == p.label:
                    pairs.append((r, p))
                    break
        return pairs


# ============================================================
# Statistical utilities
# ============================================================


def fmt_ms(value):
    if value < 1.0:
        return f"{value:.2f}"
    return f"{value:.1f}"


def speedup(tdxpy_ms, tdxrs_ms):
    if tdxrs_ms <= 0:
        return "N/A"
    ratio = tdxpy_ms / tdxrs_ms
    return f"{ratio:.1f}x"


def compute_overall_stats(suite, method_filter=None):
    rs_times = []
    py_times = []
    for r in suite.results:
        if r.skipped or r.error or not r.times_ms:
            continue
        if method_filter and r.method != method_filter:
            continue
        if r.library == "tdxrs":
            rs_times.extend(r.times_ms)
        elif r.library == "tdxpy":
            py_times.extend(r.times_ms)
    return rs_times, py_times


# ============================================================
# Connection helpers
# ============================================================


def connect_tdxrs(ip, port, timeout=5.0):
    from tdxrs import TdxHqClient
    client = TdxHqClient()
    client.connect(ip, port, timeout=timeout)
    return client


def connect_tdxpy(ip, port, timeout=5.0):
    sys.path.insert(0, str(TDXPY_DIR.parent))
    from tdxpy.hq import TdxHq_API
    client = TdxHq_API()
    client.connect(ip, port, time_out=timeout)
    return client


def safe_call(func, *args, **kwargs):
    try:
        result = func(*args, **kwargs)
        return result, None
    except Exception as e:
        return None, str(e)


# ============================================================
# Local file benchmarks
# ============================================================

DAILY_STOCKS = ["600519", "000858", "300750", "000001"]
MIN_STOCKS = ["600519", "000858", "300750"]


def bench_local_daily(rounds, warmup):
    results = []
    try:
        from tdxrs import DailyBarReader as RsReader
        from tdxpy.reader import TdxDailyBarReader as PyReader
    except ImportError as e:
        print(f"  Import error: {e}")
        return results

    rs_reader = RsReader(coefficient=0.01)
    py_reader = PyReader()

    for code in DAILY_STOCKS:
        path = str(FIXTURE_DIR / f"{code}.day")
        if not Path(path).exists():
            print(f"  {code}: fixture not found, skip")
            continue

        file_size = Path(path).stat().st_size
        est_records = file_size // 32
        label = f"{code} ({est_records} rec, {file_size}B)"

        for _ in range(warmup):
            rs_reader.parse_file(path)
            list(py_reader.parse_data_by_file(path))

        rs_times = []
        for _ in range(rounds):
            t0 = time.perf_counter()
            data = rs_reader.parse_file(path)
            rs_times.append((time.perf_counter() - t0) * 1000)
        results.append(BenchmarkResult("tdxrs", "DailyBarReader", label, rs_times, len(data)))

        py_times = []
        for _ in range(rounds):
            t0 = time.perf_counter()
            data = list(py_reader.parse_data_by_file(path))
            py_times.append((time.perf_counter() - t0) * 1000)
        results.append(BenchmarkResult("tdxpy", "DailyBarReader", label, py_times, len(data)))

    return results


def bench_local_minbar(rounds, warmup):
    results = []
    try:
        from tdxrs import MinBarReader as RsReader
        from tdxpy.reader import TdxMinBarReader as PyReader
    except ImportError as e:
        print(f"  Import error: {e}")
        return results

    rs_reader = RsReader()
    py_reader = PyReader()

    for code in MIN_STOCKS:
        path = str(FIXTURE_DIR / f"{code}.lc5")
        if not Path(path).exists():
            print(f"  {code}: fixture not found, skip")
            continue

        file_size = Path(path).stat().st_size
        est_records = file_size // 32
        label = f"{code} ({est_records} rec, {file_size}B)"

        for _ in range(warmup):
            rs_reader.parse_file(path)
            py_reader.parse_data_by_file(path)

        rs_times = []
        for _ in range(rounds):
            t0 = time.perf_counter()
            data = rs_reader.parse_file(path)
            rs_times.append((time.perf_counter() - t0) * 1000)
        results.append(BenchmarkResult("tdxrs", "MinBarReader(.lc5)", label, rs_times, len(data)))

        py_times = []
        for _ in range(rounds):
            t0 = time.perf_counter()
            data = py_reader.parse_data_by_file(path)
            py_times.append((time.perf_counter() - t0) * 1000)
        results.append(BenchmarkResult("tdxpy", "MinBarReader(.lc5)", label, py_times, len(data)))

    return results


def bench_local_lcminbar(rounds, warmup):
    results = []
    try:
        from tdxrs import LcMinBarReader as RsReader
        from tdxpy.reader import TdxLCMinBarReader as PyReader
    except ImportError as e:
        print(f"  Import error: {e}")
        return results

    rs_reader = RsReader()
    py_reader = PyReader()

    for code in MIN_STOCKS:
        path = str(FIXTURE_DIR / f"{code}_1min.lc1")
        if not Path(path).exists():
            print(f"  {code}: fixture not found, skip")
            continue

        file_size = Path(path).stat().st_size
        est_records = file_size // 32
        label = f"{code} ({est_records} rec, {file_size}B)"

        for _ in range(warmup):
            rs_reader.parse_file(path)
            py_reader.parse_data_by_file(path)

        rs_times = []
        for _ in range(rounds):
            t0 = time.perf_counter()
            data = rs_reader.parse_file(path)
            rs_times.append((time.perf_counter() - t0) * 1000)
        results.append(BenchmarkResult("tdxrs", "LcMinBarReader(.lc1)", label, rs_times, len(data)))

        py_times = []
        for _ in range(rounds):
            t0 = time.perf_counter()
            data = py_reader.parse_data_by_file(path)
            py_times.append((time.perf_counter() - t0) * 1000)
        results.append(BenchmarkResult("tdxpy", "LcMinBarReader(.lc1)", label, py_times, len(data)))

    return results


def bench_local_block(rounds, warmup):
    results = []
    try:
        from tdxrs import BlockReader as RsReader
        from tdxpy.reader import BlockReader as PyReader
    except ImportError as e:
        print(f"  Import error: {e}")
        return results

    path = str(FIXTURE_DIR / "test_block.dat")
    if not Path(path).exists():
        print("  test_block.dat not found, skip")
        return results

    file_size = Path(path).stat().st_size
    label = f"test_block.dat ({file_size}B)"

    rs_reader = RsReader()

    for _ in range(warmup):
        rs_reader.parse_file(path)
        PyReader.get_data(path)

    rs_times = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        data = rs_reader.parse_file(path)
        rs_times.append((time.perf_counter() - t0) * 1000)
    results.append(BenchmarkResult("tdxrs", "BlockReader", label, rs_times, len(data)))

    py_times = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        data = PyReader.get_data(path)
        py_times.append((time.perf_counter() - t0) * 1000)
    results.append(BenchmarkResult("tdxpy", "BlockReader", label, py_times, len(data)))

    return results


def bench_local_financial(rounds, warmup):
    results = []
    try:
        from tdxrs import FinancialReader as RsReader
        from tdxpy.reader import HistoryFinancialReader as PyReader
    except ImportError as e:
        print(f"  Import error: {e}")
        return results

    path = str(FIXTURE_DIR / "test_finance.dat")
    if not Path(path).exists():
        print("  test_finance.dat not found, skip")
        return results

    file_size = Path(path).stat().st_size
    label = f"test_finance.dat ({file_size}B)"

    rs_reader = RsReader()
    py_reader = PyReader()

    for _ in range(warmup):
        rs_reader.parse_file(path)
        py_reader.get_df(path)

    rs_times = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        data = rs_reader.parse_file(path)
        rs_times.append((time.perf_counter() - t0) * 1000)
    results.append(BenchmarkResult("tdxrs", "FinancialReader", label, rs_times, len(data)))

    py_times = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        data = py_reader.get_df(path)
        py_times.append((time.perf_counter() - t0) * 1000)
    record_count = len(data) if hasattr(data, "__len__") else 0
    results.append(BenchmarkResult("tdxpy", "FinancialReader", label, py_times, record_count))

    return results


def bench_local_all(rounds, warmup):
    suite = BenchmarkSuite(config={"rounds": rounds, "warmup": warmup, "mode": "local"})

    readers = [
        ("DailyBarReader (.day)", bench_local_daily),
        ("MinBarReader (.lc5)", bench_local_minbar),
        ("LcMinBarReader (.lc1)", bench_local_lcminbar),
        ("BlockReader (.dat)", bench_local_block),
        ("FinancialReader (.dat)", bench_local_financial),
    ]

    for name, fn in readers:
        print(f"\n  {name}:")
        r = fn(rounds, warmup)
        for item in r:
            suite.add(item)

    return suite


# ============================================================
# Network benchmarks
# ============================================================

SERVER = ("218.75.126.9", 7709)


def bench_network_connection(rounds, warmup):
    results = []
    print("  Connection time (connect+disconnect):")

    for lib_name, connect_fn in [("tdxrs", connect_tdxrs), ("tdxpy", connect_tdxpy)]:
        for _ in range(warmup):
            try:
                c = connect_fn(*SERVER)
                c.disconnect()
            except Exception:
                pass

        times = []
        for _ in range(rounds):
            t0 = time.perf_counter()
            try:
                c = connect_fn(*SERVER)
                c.disconnect()
                times.append((time.perf_counter() - t0) * 1000)
            except Exception as e:
                times.append((time.perf_counter() - t0) * 1000)

        label = f"connect+handshake"
        results.append(BenchmarkResult(lib_name, "connection", label, times))
        mean = statistics.mean(times) if times else 0
        print(f"    {lib_name}: {fmt_ms(mean)} ms")

    return results


def bench_network_all(rounds, warmup):
    suite = BenchmarkSuite(config={"rounds": rounds, "warmup": warmup, "mode": "network"})

    # Connection benchmarks
    conn_results = bench_network_connection(rounds, warmup)
    for r in conn_results:
        suite.add(r)

    # Connect both clients
    print("\n  Connecting clients...")
    try:
        rs_client = connect_tdxrs(*SERVER)
        rs_client.set_cache_ttl(0)  # disable cache for fair comparison
        print("    tdxrs connected (cache disabled)")
    except Exception as e:
        print(f"    tdxrs connect failed: {e}")
        return suite

    try:
        py_client = connect_tdxpy(*SERVER)
        print("    tdxpy connected")
    except Exception as e:
        print(f"    tdxpy connect failed: {e}")
        rs_client.disconnect()
        return suite

    try:
        _run_network_methods(suite, rs_client, py_client, rounds, warmup)
    finally:
        rs_client.disconnect()
        try:
            py_client.disconnect()
        except Exception:
            pass

    return suite


def _run_network_methods(suite, rs_client, py_client, rounds, warmup):
    methods = _build_network_methods()

    total = sum(len(m["cases"]) for m in methods)
    idx = 0

    for method_cfg in methods:
        method_name = method_cfg["name"]
        print(f"\n  [{method_name}]")

        for case in method_cfg["cases"]:
            idx += 1
            label = case["label"]
            rs_call = case["rs"]
            py_call = case["py"]

            # Warmup
            for _ in range(warmup):
                safe_call(rs_call, rs_client)
                safe_call(py_call, py_client)

            # Measure with alternation
            rs_times = []
            py_times = []
            rs_last = None
            py_last = None
            rs_err = None
            py_err = None

            for _ in range(rounds):
                # tdxrs call
                t0 = time.perf_counter()
                r, err = safe_call(rs_call, rs_client)
                elapsed = (time.perf_counter() - t0) * 1000
                if err:
                    rs_err = err
                else:
                    rs_last = r
                    rs_times.append(elapsed)

                # tdxpy call
                t0 = time.perf_counter()
                p, err = safe_call(py_call, py_client)
                elapsed = (time.perf_counter() - t0) * 1000
                if err:
                    py_err = err
                else:
                    py_last = p
                    py_times.append(elapsed)

            rs_count = len(rs_last) if isinstance(rs_last, list) else (1 if rs_last is not None else 0)
            py_count = len(py_last) if isinstance(py_last, list) else (1 if py_last is not None else 0)

            rs_result = BenchmarkResult("tdxrs", method_name, label, rs_times, rs_count,
                                        error=rs_err, skipped=not rs_times)
            py_result = BenchmarkResult("tdxpy", method_name, label, py_times, py_count,
                                        error=py_err, skipped=not py_times)

            suite.add(rs_result)
            suite.add(py_result)

            rs_mean = rs_result.mean_ms
            py_mean = py_result.mean_ms
            sp = speedup(py_mean, rs_mean) if not rs_result.skipped and not py_result.skipped else "N/A"
            print(f"    [{idx}/{total}] {label}: tdxrs={fmt_ms(rs_mean)}ms  tdxpy={fmt_ms(py_mean)}ms  {sp}")


def _build_network_methods():
    stocks_3 = [(1, "600519", "SH 600519"), (0, "000858", "SZ 000858"), (0, "300750", "SZ 300750")]
    indices = [(1, "000001", "SH 000001"), (0, "399001", "SZ 399001")]

    methods = []

    # 1. get_security_bars
    cases = []
    for cat, cat_name in [(4, "daily"), (0, "5min"), (8, "1min"), (9, "daily(cat9)")]:
        for count in [10, 100, 800, 2000]:
            if count > 800 and cat in (0, 8):
                continue  # intraday types limited to 800
            mkt, code, lbl = stocks_3[0]
            label = f"{lbl} {cat_name} count={count}"
            cases.append({
                "label": label,
                "rs": lambda c, mk=mkt, co=code, ca=cat, cnt=count: c.get_security_bars(ca, mk, co, 0, cnt),
                "py": lambda c, mk=mkt, co=code, ca=cat, cnt=count: c.get_security_bars(ca, mk, co, 0, cnt),
            })
    # Different stocks with count=800
    for mkt, code, lbl in stocks_3[1:]:
        label = f"{lbl} daily count=800"
        cases.append({
            "label": label,
            "rs": lambda c, mk=mkt, co=code: c.get_security_bars(4, mk, co, 0, 800),
            "py": lambda c, mk=mkt, co=code: c.get_security_bars(4, mk, co, 0, 800),
        })
    methods.append({"name": "get_security_bars", "cases": cases})

    # 2. get_index_bars
    cases = []
    for cat, cat_name in [(4, "daily"), (0, "5min")]:
        for mkt, code, lbl in indices:
            for count in [10, 800]:
                if count > 800 and cat == 0:
                    continue
                label = f"{lbl} {cat_name} count={count}"
                cases.append({
                    "label": label,
                    "rs": lambda c, mk=mkt, co=code, ca=cat, cnt=count: c.get_index_bars(ca, mk, co, 0, cnt),
                    "py": lambda c, mk=mkt, co=code, ca=cat, cnt=count: c.get_index_bars(ca, mk, co, 0, cnt),
                })
    methods.append({"name": "get_index_bars", "cases": cases})

    # 3. get_security_quotes
    cases = []
    for batch_size, stock_subset, lbl in [
        (1, stocks_3[:1], "1 stock"),
        (3, stocks_3, "3 stocks"),
    ]:
        stock_list = [(m, c) for m, c, _ in stock_subset]
        label = f"batch={batch_size} ({lbl})"
        cases.append({
            "label": label,
            "rs": lambda c, sl=stock_list: c.get_security_quotes(sl),
            "py": lambda c, sl=stock_list: c.get_security_quotes(sl),
        })

    # 10-stock batch: add well-known codes
    ten_stocks = [(m, c) for m, c, _ in stocks_3] + [
        (1, "601318"), (1, "600036"), (0, "000002"),
        (1, "600276"), (0, "002594"), (1, "601166"), (0, "000333"),
    ]
    cases.append({
        "label": "batch=10",
        "rs": lambda c, sl=ten_stocks: c.get_security_quotes(sl),
        "py": lambda c, sl=ten_stocks: c.get_security_quotes(sl),
    })
    methods.append({"name": "get_security_quotes", "cases": cases})

    # 4. get_security_list
    cases = []
    for mkt, mkt_name in [(0, "SZ"), (1, "SH")]:
        label = f"{mkt_name} start=0"
        cases.append({
            "label": label,
            "rs": lambda c, mk=mkt: c.get_security_list(mk, 0),
            "py": lambda c, mk=mkt: c.get_security_list(mk, 0),
        })
    methods.append({"name": "get_security_list", "cases": cases})

    # 5. get_security_count
    cases = []
    for mkt, mkt_name in [(0, "SZ"), (1, "SH")]:
        label = f"{mkt_name}"
        cases.append({
            "label": label,
            "rs": lambda c, mk=mkt: c.get_security_count(mk),
            "py": lambda c, mk=mkt: c.get_security_count(mk),
        })
    methods.append({"name": "get_security_count", "cases": cases})

    # 6. get_minute_time_data
    cases = []
    for mkt, code, lbl in stocks_3:
        label = f"{lbl}"
        cases.append({
            "label": label,
            "rs": lambda c, mk=mkt, co=code: c.get_minute_time_data(mk, co),
            "py": lambda c, mk=mkt, co=code: c.get_minute_time_data(mk, co),
        })
    methods.append({"name": "get_minute_time_data", "cases": cases})

    # 7. get_history_minute_time_data
    cases = []
    for mkt, code, lbl in stocks_3:
        label = f"{lbl} date=20260430"
        cases.append({
            "label": label,
            "rs": lambda c, mk=mkt, co=code: c.get_history_minute_time_data(mk, co, 20260430),
            "py": lambda c, mk=mkt, co=code: c.get_history_minute_time_data(mk, co, 20260430),
        })
    methods.append({"name": "get_history_minute_time_data", "cases": cases})

    # 8. get_transaction_data
    cases = []
    for count in [10, 100, 1000]:
        mkt, code, lbl = stocks_3[0]
        label = f"{lbl} count={count}"
        cases.append({
            "label": label,
            "rs": lambda c, mk=mkt, co=code, cnt=count: c.get_transaction_data(mk, co, 0, cnt),
            "py": lambda c, mk=mkt, co=code, cnt=count: c.get_transaction_data(mk, co, 0, cnt),
        })
    methods.append({"name": "get_transaction_data", "cases": cases})

    # 9. get_history_transaction_data
    cases = []
    for count in [10, 100, 1000]:
        mkt, code, lbl = stocks_3[0]
        label = f"{lbl} count={count} date=20260430"
        cases.append({
            "label": label,
            "rs": lambda c, mk=mkt, co=code, cnt=count: c.get_history_transaction_data(mk, co, 0, cnt, 20260430),
            "py": lambda c, mk=mkt, co=code, cnt=count: c.get_history_transaction_data(mk, co, 0, cnt, 20260430),
        })
    methods.append({"name": "get_history_transaction_data", "cases": cases})

    # 10. get_finance_info
    cases = []
    for mkt, code, lbl in stocks_3:
        label = f"{lbl}"
        cases.append({
            "label": label,
            "rs": lambda c, mk=mkt, co=code: c.get_finance_info(mk, co),
            "py": lambda c, mk=mkt, co=code: c.get_finance_info(mk, co),
        })
    methods.append({"name": "get_finance_info", "cases": cases})

    # 11. get_xdxr_info
    cases = []
    for mkt, code, lbl in stocks_3:
        label = f"{lbl}"
        cases.append({
            "label": label,
            "rs": lambda c, mk=mkt, co=code: c.get_xdxr_info(mk, co),
            "py": lambda c, mk=mkt, co=code: c.get_xdxr_info(mk, co),
        })
    methods.append({"name": "get_xdxr_info", "cases": cases})

    # 12. get_and_parse_block_info
    cases = [{
        "label": "block_gn.dat",
        "rs": lambda c: c.get_and_parse_block_info("block_gn.dat"),
        "py": lambda c: c.get_and_parse_block_info("block_gn.dat"),
    }]
    methods.append({"name": "get_and_parse_block_info", "cases": cases})

    # tdxrs-only: get_security_bars_all
    cases = []
    for count in [1000, 2000]:
        label = f"SH 600519 daily count={count} (auto-paginate)"
        cases.append({
            "label": label,
            "rs": lambda c, cnt=count: c.get_security_bars_all(4, 1, "600519", cnt),
            "py": lambda c, cnt=count: None,  # not available in tdxpy
        })
    methods.append({"name": "get_security_bars_all", "cases": cases, "tdxrs_only": True})

    return methods


# ============================================================
# Report generator
# ============================================================


def generate_report(suite_local, suite_network, output_path, config):
    lines = []
    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    lines.append("# tdxrs vs tdxpy Performance Benchmark Report\n")
    lines.append(f"**Generated**: {ts}")
    lines.append(f"**Configuration**: rounds={config['rounds']}, warmup={config['warmup']}, mode={config['mode']}")
    if suite_network and suite_network.results:
        lines.append(f"**Server**: {SERVER[0]}:{SERVER[1]}")
    lines.append("")
    lines.append("---\n")

    # Executive summary
    lines.append("## Executive Summary\n")

    net_rs, net_py = compute_overall_stats(suite_network)
    local_rs, local_py = compute_overall_stats(suite_local)

    lines.append("| Category | tdxrs Avg (ms) | tdxpy Avg (ms) | Speedup |")
    lines.append("|----------|---------------|---------------|---------|")

    if net_rs and net_py:
        net_rs_avg = statistics.mean(net_rs)
        net_py_avg = statistics.mean(net_py)
        lines.append(f"| Network API | {fmt_ms(net_rs_avg)} | {fmt_ms(net_py_avg)} | {speedup(net_py_avg, net_rs_avg)} |")
    elif suite_network:
        lines.append("| Network API | (skipped) | (skipped) | N/A |")

    if local_rs and local_py:
        local_rs_avg = statistics.mean(local_rs)
        local_py_avg = statistics.mean(local_py)
        lines.append(f"| Local Readers | {fmt_ms(local_rs_avg)} | {fmt_ms(local_py_avg)} | {speedup(local_py_avg, local_rs_avg)} |")
    elif suite_local:
        lines.append("| Local Readers | (skipped) | (skipped) | N/A |")

    all_rs = net_rs + local_rs
    all_py = net_py + local_py
    if all_rs and all_py:
        all_rs_avg = statistics.mean(all_rs)
        all_py_avg = statistics.mean(all_py)
        lines.append(f"| **Overall** | **{fmt_ms(all_rs_avg)}** | **{fmt_ms(all_py_avg)}** | **{speedup(all_py_avg, all_rs_avg)}** |")

    lines.append("")

    # Methodology
    lines.append("## Methodology\n")
    lines.append(f"- Each benchmark runs **{config['warmup']} warmup rounds** (discarded) followed by **{config['rounds']} measurement rounds**")
    lines.append("- Timing uses `time.perf_counter()` (nanosecond resolution)")
    lines.append("- Network benchmarks **alternate** tdxrs/tdxpy calls within each round to minimize network condition drift")
    lines.append("- Connection time is measured **separately** from API call time")
    lines.append("- Statistics: mean, min, max, std deviation, p95")
    lines.append("- Speedup = tdxpy_mean / tdxrs_mean (> 1.0 means tdxrs is faster)")
    lines.append("")

    # Part 1: Network
    if suite_network and suite_network.results:
        lines.append("---\n")
        lines.append("## Part 1: Network API Benchmarks\n")

        # Connection
        conn_pairs = suite_network.get_pairs("connection")
        if conn_pairs:
            lines.append("### 1.1 Connection Time\n")
            lines.append("| Library | Mean (ms) | Min (ms) | Max (ms) | Std (ms) | P95 (ms) |")
            lines.append("|---------|-----------|----------|----------|----------|----------|")
            for r, p in conn_pairs:
                lines.append(f"| {r.library} | {fmt_ms(r.mean_ms)} | {fmt_ms(r.min_ms)} | {fmt_ms(r.max_ms)} | {fmt_ms(r.std_ms)} | {fmt_ms(r.p95_ms)} |")
            if len(conn_pairs) == 2:
                lines.append(f"\n**Speedup**: {speedup(conn_pairs[1][1].mean_ms, conn_pairs[0][1].mean_ms)}")
            lines.append("")

        # Per-method tables
        methods_seen = set()
        for r in suite_network.results:
            if r.method not in ("connection",) and r.method not in methods_seen:
                methods_seen.add(r.method)

        section_num = 2
        for method_name in sorted(methods_seen):
            pairs = suite_network.get_pairs(method_name)
            if not pairs:
                continue

            lines.append(f"### 1.{section_num} {method_name}\n")

            is_tdxrs_only = any(r.skipped for r, _ in pairs if r.library == "tdxpy")

            if is_tdxrs_only:
                lines.append("| Label | tdxrs (ms) | Records | Notes |")
                lines.append("|-------|------------|---------|-------|")
                for r, p in pairs:
                    if r.library == "tdxrs":
                        lines.append(f"| {r.label} | {fmt_ms(r.mean_ms)} | {r.record_count} | tdxrs-only |")
            else:
                lines.append("| Label | tdxrs (ms) | tdxpy (ms) | Speedup | Records |")
                lines.append("|-------|------------|------------|---------|---------|")
                for r, p in pairs:
                    sp = speedup(p.mean_ms, r.mean_ms) if not r.skipped and not p.skipped else "N/A"
                    note = ""
                    if p.error:
                        note = f" (py err: {p.error[:30]})"
                    lines.append(f"| {r.label} | {fmt_ms(r.mean_ms)} | {fmt_ms(p.mean_ms)} | {sp} | {r.record_count}/{p.record_count} |")

            lines.append("")
            section_num += 1

    # Part 2: Local
    if suite_local and suite_local.results:
        lines.append("---\n")
        lines.append("## Part 2: Local File Reader Benchmarks\n")

        readers_seen = []
        for r in suite_local.results:
            if r.method not in [x for x in readers_seen]:
                readers_seen.append(r.method)

        section_num = 1
        for reader_name in readers_seen:
            pairs = suite_local.get_pairs(reader_name)
            if not pairs:
                continue

            lines.append(f"### 2.{section_num} {reader_name}\n")
            lines.append("| Label | tdxrs (ms) | tdxpy (ms) | Speedup | Records |")
            lines.append("|-------|------------|------------|---------|---------|")
            for r, p in pairs:
                sp = speedup(p.mean_ms, r.mean_ms)
                lines.append(f"| {r.label} | {fmt_ms(r.mean_ms)} | {fmt_ms(p.mean_ms)} | {sp} | {r.record_count}/{p.record_count} |")
            lines.append("")
            section_num += 1

    # Part 3: Analysis
    lines.append("---\n")
    lines.append("## Part 3: Analysis and Optimization Recommendations\n")

    lines.append("### Key Findings\n")

    if all_rs and all_py:
        all_rs_avg = statistics.mean(all_rs)
        all_py_avg = statistics.mean(all_py)
        overall_sp = all_py_avg / all_rs_avg if all_rs_avg > 0 else 0
        lines.append(f"1. **Overall speedup: {overall_sp:.1f}x** — tdxrs is {overall_sp:.1f}x faster than tdxpy across all benchmarks")

    if net_rs and net_py:
        net_rs_avg = statistics.mean(net_rs)
        net_py_avg = statistics.mean(net_py)
        net_sp = net_py_avg / net_rs_avg if net_rs_avg > 0 else 0
        lines.append(f"2. **Network API speedup: {net_sp:.1f}x** — protocol parsing overhead reduction")

    if local_rs and local_py:
        local_rs_avg = statistics.mean(local_rs)
        local_py_avg = statistics.mean(local_py)
        local_sp = local_py_avg / local_rs_avg if local_rs_avg > 0 else 0
        lines.append(f"3. **Local reader speedup: {local_sp:.1f}x** — file parsing performance gain")

    lines.append("")
    lines.append("### Performance Characteristics\n")
    lines.append("- **Network-bound operations**: Where network latency dominates, speedup reflects parsing overhead reduction")
    lines.append("- **Parse-bound operations**: Local file readers show pure computational speedup (no network variance)")
    lines.append("- **Scaling behavior**: Larger data volumes typically show higher speedup due to amortized fixed costs")
    lines.append("")

    lines.append("### Optimization Recommendations\n")
    lines.append("Based on the benchmark results:\n")
    lines.append("1. **Connection pool warmup**: Pre-establish connections to eliminate cold-start latency")
    lines.append("2. **Batch request merging**: Combine multiple small requests where possible")
    lines.append("3. **Zero-copy parsing**: For very large datasets, consider avoiding intermediate Vec allocation")
    lines.append("4. **SIMD acceleration**: Price delta decoding could benefit from vectorized operations")
    lines.append("5. **Async I/O**: Enable tokio-based async client for concurrent multi-stock queries")
    lines.append("")

    lines.append("---\n")
    lines.append("## Statistical Methodology\n")
    lines.append(f"- **Warmup**: {config['warmup']} rounds discarded to eliminate JIT/cache cold-start effects")
    lines.append(f"- **Measurement**: {config['rounds']} rounds timed with `time.perf_counter()`")
    lines.append("- **Alternation**: Network benchmarks alternate calls within each round to minimize temporal network bias")
    lines.append("- **Reported values**: Mean of measurement rounds; Std measures consistency; P95 captures tail latency")
    lines.append("- **Speedup**: `tdxpy_mean / tdxrs_mean`; values > 1.0 mean tdxrs is faster")
    lines.append("- **Limitations**: Network benchmarks subject to server load and latency variance; local benchmarks are deterministic")
    lines.append("")

    report = "\n".join(lines)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)

    return report


# ============================================================
# CLI and main
# ============================================================


def main():
    parser = argparse.ArgumentParser(description="tdxrs vs tdxpy Performance Benchmark")
    parser.add_argument("--mode", choices=["local", "network", "all"], default="all")
    parser.add_argument("--rounds", type=int, default=20, help="Measurement rounds (default: 20)")
    parser.add_argument("--warmup", type=int, default=3, help="Warmup rounds (default: 3)")
    parser.add_argument("--output", type=str, default=str(REPORT_DIR / "BENCHMARK_REPORT.md"))
    parser.add_argument("--no-report", action="store_true", help="Skip report generation")
    args = parser.parse_args()

    print("=" * 60)
    print("  tdxrs vs tdxpy Performance Benchmark")
    print(f"  Mode: {args.mode} | Rounds: {args.rounds} | Warmup: {args.warmup}")
    print("=" * 60)

    t_start = time.time()
    suite_local = BenchmarkSuite()
    suite_network = BenchmarkSuite()

    if args.mode in ("local", "all"):
        print("\n[Local] Benchmarking file readers...")
        suite_local = bench_local_all(args.rounds, args.warmup)

    if args.mode in ("network", "all"):
        print("\n[Network] Benchmarking API methods...")
        suite_network = bench_network_all(args.rounds, args.warmup)

    elapsed = time.time() - t_start
    print(f"\n{'=' * 60}")
    print(f"  Benchmark complete. Total time: {elapsed:.1f}s")

    if not args.no_report:
        print(f"\n  Generating report: {args.output}")
        config = {"rounds": args.rounds, "warmup": args.warmup, "mode": args.mode}
        generate_report(suite_local, suite_network, args.output, config)
        print(f"  Report saved to: {args.output}")

    print("=" * 60)


if __name__ == "__main__":
    main()
