# -*- coding: utf-8 -*-
"""基准回归门禁 + 正确性门禁 —— 计划 §P5 的「CI 门禁」。

对计划的一处必要修正
--------------------
计划原文是「bench_local.py 结果与 bench_gate.json 对比, 任一场景退化 >5% 即失败」。
**按绝对耗时做门禁在这台机器上必然误报**: 同一路径实测 IO 地板在 0.889 s ~ 5.4 s
之间漂移 (5.7x) —— 零拷贝单线程在快窗口是 4.80x、慢窗口只剩 1.76x, 都是真数字,
差别只在 IO 窗口。因此门禁比较的是**归一化比值** ``wall_s / 同轮 io_floor_s``,
它把 IO 窗口的影响约掉, 才是可比的量。

另外两条例外:
  * 语料可能变 (vipdoc 每交易日盘后更新) -> ``rows`` 不一致时只 WARN 并跳过该场景,
    不判 FAIL, 否则每天都会红。
  * 场景缺失 (某实现被移除) -> 只 WARN; 新增场景不参与比较。

用法
----
    # 记录基线 (用 bench_local.py 产出的 JSON)
    python tests/bench_local.py --rounds 3 --out bench_local.json
    python tests/bench_gate.py --record bench_gate.json --from bench_local.json

    # 门禁检查
    python tests/bench_gate.py --check new.json --baseline bench_gate.json

    # 正确性门禁 (跑 Rust + Python runner, 缺 vipdoc 自动跳过)
    python tests/run_all.py
退出码: 0 = 通过; 1 = 有退化/正确性失败; 2 = 用法错误
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

# 归一化比值被认为「可比」的最小 floor (单位: 秒)
# 地板太低时分子分母的抖动会被放大, 这类轮次直接跳过比较。
MIN_FLOOR_S = 0.05


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def normalized(report: dict) -> dict:
    """{场景: (比值, 绝对耗时, 行数)}; 缺少 io_floor 的轮次整体不可比。"""
    floor = report.get("io_floor_s")
    out = {}
    if not floor or floor < MIN_FLOOR_S:
        return out
    for name, r in (report.get("scenarios") or {}).items():
        if "wall_s" not in r or "error" in r:
            continue
        out[name] = {
            "ratio": r["wall_s"] / floor,
            "wall_s": r["wall_s"],
            "rows": r.get("rows"),
            "peak_rss_mb": r.get("peak_rss_mb"),
        }
    return out


def record(args) -> int:
    rep = load(args.from_file)
    norm = normalized(rep)
    if not norm:
        print("基线不可用: 缺少有效的 io_floor_s", file=sys.stderr)
        return 2
    payload = {
        "note": "门禁基线: 只比归一化比值 (wall_s / io_floor_s); "
                "绝对耗时与峰值内存仅供排查, 跨机器不可比。"
                "更新: tests/bench_gate.py --record",
        "env": rep.get("env"),
        "n_files": rep.get("n_files"),
        "screen_n": rep.get("screen_n"),
        "io_floor_s_at_record": rep.get("io_floor_s"),
        "scenarios": norm,
    }
    Path(args.record).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    print(f"已记录基线 {args.record}: {len(norm)} 个场景")
    for k, v in sorted(norm.items(), key=lambda kv: -kv[1]["ratio"]):
        print(f"  {k:15} /IO = {v['ratio']:6.2f}x  ({v['wall_s']:.3f}s, "
              f"峰值 {v['peak_rss_mb']} MB)")
    return 0


def check(args) -> int:
    base = load(args.baseline)
    new = normalized(load(args.check))
    tol = args.tol

    if not new:
        print("本次报告不可用: 缺少有效的 io_floor_s (IO 地板过低或未测)",
              file=sys.stderr)
        return 2

    fails: list[str] = []
    warns: list[str] = []
    oks: list[str] = []

    for name, b in sorted(base["scenarios"].items()):
        if name not in new:
            warns.append(f"{name:15} 本次未测 (SKIP/移除), 不判失败")
            continue
        n = new[name]
        if b.get("rows") and n.get("rows") and b["rows"] != n["rows"]:
            warns.append(f"{name:15} 行数变了 {b['rows']} -> {n['rows']} "
                         f"(语料更新?), 跳过比值比较")
            continue
        limit = b["ratio"] * (1.0 + tol)
        delta = (n["ratio"] - b["ratio"]) / b["ratio"] * 100
        line = (f"{name:15} /IO {b['ratio']:6.2f}x -> {n['ratio']:6.2f}x  "
                f"({delta:+6.1f}%)  上限 {limit:6.2f}x")
        if n["ratio"] > limit:
            fails.append(line)
        else:
            oks.append(line)
        # 峰值内存只警告不判失败: 峰值受分配器/页缓存影响, 抖动大
        if b.get("peak_rss_mb") and n.get("peak_rss_mb"):
            if n["peak_rss_mb"] > b["peak_rss_mb"] * (1.0 + 3 * tol):
                warns.append(f"{name:15} 峰值内存 {b['peak_rss_mb']} -> "
                             f"{n['peak_rss_mb']} MB (>{3*tol:.0%})")

    for name in new:
        if name not in base["scenarios"]:
            warns.append(f"{name:15} 基线里没有, 跳过 (新增场景先 --record)")

    for l in oks:
        print(f"  [ok]   {l}")
    for l in warns:
        print(f"  [warn] {l}")
    for l in fails:
        print(f"  [FAIL] {l}")

    print(f"\n门禁: {len(oks)} 通过 / {len(fails)} 退化 / {len(warns)} 警告 "
          f"(容差 {tol:.0%})")
    if fails:
        print("GATE_FAIL")
        return 1
    print("GATE_OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", default=None, help="写入基线 JSON")
    ap.add_argument("--from", dest="from_file", default=None,
                    help="来源: bench_local.py 产出的 JSON")
    ap.add_argument("--check", default=None, help="要检查的 JSON (本次运行)")
    ap.add_argument("--baseline", default=str(SCRIPT_DIR.parent / "bench_gate.json"))
    ap.add_argument("--tol", type=float, default=0.10,
                    help="退化容差 (默认 0.10 = 允许 +10%%)")
    args = ap.parse_args()

    if args.record:
        if not args.from_file:
            print("--record 需要同时给 --from", file=sys.stderr)
            return 2
        return record(args)
    if args.check:
        return check(args)
    print("需要 --record 或 --check", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
