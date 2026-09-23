# -*- coding: utf-8 -*-
"""tdxrs 热路径性能基准（重构回归裁判）

用法:
    python examples/bench_hotpath.py                 # 默认 20 万条 x 5 轮
    python examples/bench_hotpath.py --bars 50000 --rounds 3
    python examples/bench_hotpath.py --json out.json # 输出机器可读结果

测量 .day 日线文件解析三种输出模式的吞吐:
    parse_data (list[dict]) / parse_data_tuples (list[tuple]) / to_dataframe (pandas)

回归门槛: 任一模式相对 BENCHMARKS_BASELINE.md 记录的基线退化 >10% 即阻断合并。
"""
import argparse
import gc
import json
import statistics
import sys
import time

import tdxrs

DEFAULT_BARS = 200_000
DEFAULT_ROUNDS = 5
RECORD_SIZE = 32


def build_day_data(n: int) -> bytes:
    """合成 n 条 .day 记录: date=(y-2004)*2048+m*100+d, 价格为整数编码"""
    import struct
    buf = bytearray()
    y, m, d = 2015, 1, 1
    for i in range(n):
        date_num = (y - 2004) * 2048 + m * 100 + d
        base = 1000 + i % 500
        amount = 1e8 + i
        vol = 10000 + i % 9000
        buf += struct.pack("<IIIIIfII", date_num, base, base + 100, base - 50, base + 50, amount, vol, 0)
        d += 1
        if d > 28:
            d = 1
            m += 1
            if m > 12:
                m = 1
                y += 1
    return bytes(buf)


def bench(fn, data: bytes, rounds: int) -> float:
    """多轮计时, 返回最优秒数 (best-of 抗抖动)"""
    times = []
    for _ in range(rounds):
        gc.collect()
        t0 = time.perf_counter()
        result = fn(data)
        dt = time.perf_counter() - t0
        times.append(dt)
        del result
    return min(times)


def approx_size(obj) -> int:
    """递归近似内存占用"""
    seen = set()
    total = 0
    stack = [obj]
    while stack:
        o = stack.pop()
        if id(o) in seen:
            continue
        seen.add(id(o))
        try:
            total += sys.getsizeof(o)
        except TypeError:
            continue
        if isinstance(o, dict):
            stack.extend(o.keys())
            stack.extend(o.values())
        elif isinstance(o, (list, tuple)):
            stack.extend(o)
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="tdxrs hot-path benchmark")
    ap.add_argument("--bars", type=int, default=DEFAULT_BARS)
    ap.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    ap.add_argument("--json", type=str, default=None, help="write results to JSON file")
    args = ap.parse_args()

    n, rounds = args.bars, args.rounds
    print(f"tdxrs {tdxrs.__version__} | {n:,} bars | {n * RECORD_SIZE / 1e6:.1f} MB | {rounds} rounds, best-of")
    print("-" * 78)

    data = build_day_data(n)
    reader = tdxrs.DailyBarReader(0.01)

    results = {"tdxrs_version": tdxrs.__version__, "bars": n, "rounds": rounds, "modes": {}}

    t = bench(reader.parse_data, data, rounds)
    results["modes"]["parse_data"] = {"sec": t, "us_per_bar": t / n * 1e6, "bars_per_s": n / t}

    t = bench(reader.parse_data_tuples, data, rounds)
    results["modes"]["parse_data_tuples"] = {"sec": t, "us_per_bar": t / n * 1e6, "bars_per_s": n / t}

    try:
        import pandas  # noqa: F401
        if not hasattr(pandas, "DataFrame"):
            raise ImportError("pandas installation incomplete")
        t = bench(reader.to_dataframe, data, rounds)
        results["modes"]["to_dataframe"] = {"sec": t, "us_per_bar": t / n * 1e6, "bars_per_s": n / t}
    except ImportError as e:
        print(f"pandas unusable ({e}) -> to_dataframe skipped")
        results["modes"]["to_dataframe"] = None

    print(f"{'mode':<38}{'best (s)':>10}{'bars/s':>14}{'us/bar':>10}")
    for name, r in results["modes"].items():
        if r is None:
            print(f"{name:<38}{'skipped':>10}")
            continue
        print(f"{name:<38}{r['sec']:>10.4f}{r['bars_per_s']:>14,.0f}{r['us_per_bar']:>10.2f}")

    # 内存估算 (1 万条缩样)
    sample = data[: 10_000 * RECORD_SIZE]
    dicts, tuples = reader.parse_data(sample), reader.parse_data_tuples(sample)
    sd, st = approx_size(dicts), approx_size(tuples)
    results["memory_10k"] = {"dict_bytes": sd, "tuple_bytes": st, "dict_bytes_per_bar": sd // 10000, "tuple_bytes_per_bar": st // 10000}
    del dicts, tuples
    print(f"memory (10k bars): dict {sd/1e6:.1f} MB ({sd//10000} B/bar) | tuple {st/1e6:.1f} MB ({st//10000} B/bar)")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        print(f"results -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
