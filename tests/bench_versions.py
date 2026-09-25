# -*- coding: utf-8 -*-
"""多版本交替基准驱动 —— 把同一批场景跑在多个**已安装构建**上, 用于版本对比报告。

为什么需要它:
  bench_local.py 一次只测"当前装的那一份", 三个版本要跑三遍 -> 三遍的 IO 窗口不同,
  而这台机器的 IO 地板在 0.9s~5.9s 间漂移(5.7x), 顺序执行会把版本差异和窗口差异混在一起。
  本驱动按**轮次交错**(round-robin): 第 r 轮里依次跑 上游 / 改造前 / 改造后,
  每个 (版本, 场景) 都有**同轮同版本**的 IO 地板, 归一化比值才是可比量。

切版本方式: 只改 `PYTHONPATH`(前缀指向解压后的 wheel 目录), 同一个解释器、同一份
numpy/pandas -> 免建多个 venv。`None` 表示用 venv 里装的那一份(即当前分支 HEAD)。

用法:
    python tests/bench_versions.py --out ../../bench_versions.json
    python tests/bench_versions.py --rounds 3 --warmup 1
    python tests/bench_versions.py --only io_floor,df_1t,df_8t
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
TDXRS = SCRIPT_DIR.parent
PY = sys.executable
PYLIBS = r"D:\Agent\TDX RS\pylibs"          # 可选依赖(pyarrow/polars/duckdb), 不碰 site-packages

# ---------------------------------------------------------------
# 参与对比的构建
# ---------------------------------------------------------------
VERSIONS: list[tuple[str, str | None]] = [
    ("upstream", r"D:\Agent\TDX RS\baseline_sp"),    # e2708cc 纯上游
    ("prelocal", r"D:\Agent\TDX RS\prelocal_sp"),    # 32d25c1 本地解析改造前
    ("current",  None),                               # 25cfbe5 改造后(venv 安装)
]

# ---------------------------------------------------------------
# 场景 -> (所需模块, 样本)
#   所需模块为 None 表示任何构建都有; 否则对应构建缺该模块则记 n/a。
#   样本: market=全市场, screen=300 只均匀抽样(与 bench_local 的 SCREEN_SCENARIOS 一致)
# ---------------------------------------------------------------
SCEN_META: dict[str, tuple[str | None, str]] = {
    # ---- 三档可比: 只用 Rust 公开 API, 三个构建都有 ----
    "io_floor":       (None, "market"),
    "tuples_1t":      (None, "market"),
    "df_1t":          (None, "market"),
    "df_8t":          (None, "market"),
    # ---- 改造前 vs 改造后: 需要 hybrid 层 ----
    "screen_legacy":  ("tdxrs.hybrid", "screen"),
    "screen_hybrid":  ("tdxrs.hybrid", "screen"),
    # ---- 仅改造后: 零拷贝 / 批量 / 筛查 ----
    "zc_1t":          ("tdxrs.local", "market"),
    "zc_8t":          ("tdxrs.local", "market"),
    "mmap_1t":        ("tdxrs.local", "market"),
    "zc_batch":       ("tdxrs.local", "market"),
    "scan_daily":     ("tdxrs.local", "market"),
    "scan_1t":        ("tdxrs.local", "market"),
    "scan_8t":        ("tdxrs.local", "market"),
    "scan_panel_df":  ("tdxrs.local", "market"),
    "screen_tail":    ("tdxrs.local", "screen"),
    "screen_scan":    ("tdxrs.local", "screen"),
    "screen_grid":    ("tdxrs.local", "screen"),
}

DESCS = {
    "io_floor": "IO 地板: read_bytes 全量",
    "tuples_1t": "现状 list[tuple] 1 线程",
    "df_1t": "现状 to_dataframe_file 1 线程",
    "df_8t": "现状 to_dataframe_file 8 线程",
    "screen_legacy": "筛查(旧行为锚点): 全量解析+切片",
    "screen_hybrid": "筛查: HybridClient(count=30)",
    "zc_1t": "零拷贝 read_daily 1 线程",
    "zc_8t": "零拷贝 read_daily 8 线程",
    "mmap_1t": "零拷贝 np.memmap 1 线程",
    "zc_batch": "零拷贝 + concat 批量拼装",
    "scan_daily": "批量 scan_daily (自动并行, 两遍法)",
    "scan_1t": "批量 scan_daily 1 线程",
    "scan_8t": "批量 scan_daily 8 线程",
    "scan_panel_df": "批量 scan_daily -> 单 DataFrame",
    "screen_tail": "筛查: read_tail(30) 串行",
    "screen_scan": "筛查: scan_daily(tail=30) 批量并行",
    "screen_grid": "筛查: scan_daily+close_grid (K,30) 网格",
}


def ver_env(sp: str | None) -> dict:
    """构造该构建的进程环境: PYTHONPATH 前缀 = wheel 目录 + 可选依赖目录。"""
    env = dict(os.environ)
    parts = []
    if sp:
        parts.append(sp)
    parts.append(PYLIBS)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def probe(env: dict) -> dict:
    """探测该构建解析到哪、版本号、以及各模块是否可用。"""
    code = (
        "import json,sys,importlib.util as u;"
        "import tdxrs;"
        "mods=['local','hybrid','boundary','arrow_cache','server_health'];"
        "print('PROBE'+json.dumps({'version':getattr(tdxrs,'__version__','?'),"
        "'path':tdxrs.__file__,"
        "'mods':{m:(u.find_spec('tdxrs.'+m) is not None) for m in mods}}))"
    )
    p = subprocess.run([PY, "-X", "utf8", "-c", code], capture_output=True, text=True,
                       cwd=str(TDXRS), env=env)
    line = next((l for l in p.stdout.splitlines() if l.startswith("PROBE")), None)
    if not line:
        return {"error": (p.stderr or "?").strip()[-400:]}
    return json.loads(line[5:])


def run_one(scenario: str, env: dict, args) -> dict:
    """单场景单轮 worker。走 bench_local.py 的 --scenario 模式, 不复制场景实现。"""
    cmd = [PY, str(SCRIPT_DIR / "bench_local.py"), "--vipdoc", args.vipdoc,
           "--scenario", scenario, "--rounds", "1", "--screen-n", str(args.screen_n)]
    if args.n:
        cmd += ["--n", str(args.n)]
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(TDXRS), env=env)
    line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT ")), None)
    if not line:
        err = (p.stderr.strip().splitlines() or ["?"])[-1]
        return {"error": err[:300]}
    return json.loads(line[7:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vipdoc", default=r"D:\TDX\vipdoc")
    ap.add_argument("--n", type=int, default=None, help="全市场样本数 (None=全部)")
    ap.add_argument("--screen-n", type=int, default=300)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--warmup", type=int, default=1, help="预热轮数(不计入结果)")
    ap.add_argument("--only", default=None, help="逗号分隔场景名")
    ap.add_argument("--out", default=str(TDXRS.parent / "bench_versions.json"))
    args = ap.parse_args()

    names = list(SCEN_META)
    if args.only:
        names = [s.strip() for s in args.only.split(",") if s.strip() in SCEN_META]
        if not names:
            sys.exit("--only 未命中任何场景")

    # ---- 探测三份构建 ----
    print("=" * 84)
    print("多版本交替基准   (轮次交错 -> 每个 (版本,场景) 用同轮 IO 地板归一化)")
    print("=" * 84)
    envs: dict[str, dict] = {}
    probes: dict[str, dict] = {}
    for label, sp in VERSIONS:
        env = ver_env(sp)
        envs[label] = env
        pr = probe(env)
        probes[label] = pr
        mods = ",".join(k for k, v in pr.get("mods", {}).items() if v) or "(仅核心)"
        print(f"  {label:10} {pr.get('version','?'):8} {Path(pr.get('path','?')).parent}   模块: {mods}")
    print("-" * 84)

    # ---- 过滤掉该构建跑不了的场景 ----
    plan: dict[str, list[str]] = {}
    for name in names:
        need, _sample = SCEN_META[name]
        if need is None:
            plan[name] = [lb for lb, _ in VERSIONS]
            continue
        short = need.split(".")[-1]
        plan[name] = [lb for lb, _ in VERSIONS if probes.get(lb, {}).get("mods", {}).get(short)]
    # io_floor 是归一化基准, 无论是否被 --only 选中都必须跑
    plan.setdefault("io_floor", [lb for lb, _ in VERSIONS])
    for name in names:
        got = plan[name]
        miss = [lb for lb, _ in VERSIONS if lb not in got]
        tag = f"   (缺: {','.join(miss)})" if miss else ""
        print(f"  {name:15} {len(got)}/{len(VERSIONS)} 档{tag}")

    # ---- 交错执行 ----
    # rounds[label][scenario] = [ {s, floor}, ... ]
    store: dict[str, dict[str, list[dict]]] = {
        lb: {nm: [] for nm in plan if lb in plan[nm]} for lb, _ in VERSIONS}
    total = args.warmup + args.rounds
    for r in range(total):
        warm = r < args.warmup
        tag = "预热" if warm else f"第 {r - args.warmup + 1}/{args.rounds} 轮"
        print("-" * 84)
        print(f"[{tag}]")
        for label, _sp in VERSIONS:
            if "io_floor" not in store[label]:
                continue
            floor = run_one("io_floor", envs[label], args)
            if "wall_s" not in floor:
                print(f"  {label:10} io_floor 失败: {floor.get('error')}")
                continue
            print(f"  {label:10} IO 地板 {floor['wall_s']:.3f}s")
            if not warm and "io_floor" in store[label]:
                store[label]["io_floor"].append({"s": floor["wall_s"], "floor": floor["wall_s"],
                                                 "rows": floor.get("rows", 0),
                                                 "peak": floor.get("peak_rss_mb", 0.0)})
            for name in names:
                if label not in plan[name] or name == "io_floor":
                    continue
                r1 = run_one(name, envs[label], args)
                if warm:
                    continue
                if "wall_s" not in r1:
                    store[label][name].append({"error": r1.get("error", "?")})
                    print(f"      {name:15} FAILED: {r1.get('error')}")
                    continue
                store[label][name].append({"s": r1["wall_s"], "floor": floor["wall_s"],
                                           "rows": r1.get("rows", 0),
                                           "peak": r1.get("peak_rss_mb", 0.0)})
                print(f"      {name:15} {r1['wall_s']:8.3f}s  /IO {r1['wall_s']/floor['wall_s']:5.2f}x")

    # ---- 汇总: 绝对耗时取 min, 归一化比值取 min(逐轮算再取 min) ----
    out: dict[str, dict] = {}
    for name in names:
        entry: dict = {"desc": DESCS.get(name, name), "requires": SCEN_META[name][0],
                       "results": {}}
        for label, _sp in VERSIONS:
            if label not in plan[name]:
                entry["results"][label] = {"unavailable": f"该构建无 {SCEN_META[name][0]}"}
                continue
            recs = [x for x in store[label][name] if "s" in x]
            if not recs:
                entry["results"][label] = {"error": "无有效轮次"}
                continue
            best = min(recs, key=lambda x: x["s"])
            ratios = [x["s"] / x["floor"] for x in recs]
            entry["results"][label] = {
                "wall_s": round(best["s"], 6),
                "ratio": round(min(ratios), 4),
                "all_s": [round(x["s"], 6) for x in recs],
                "all_ratio": [round(x, 4) for x in ratios],
                "floors": [round(x["floor"], 6) for x in recs],
                "rows": best["rows"],
                "peak_rss_mb": best["peak"],
            }
        out[name] = entry

    payload = {
        "env": {"python": sys.version.split()[0], "warmup": args.warmup},
        "versions": {lb: {"pythonpath": sp, "probe": probes[lb]} for lb, sp in VERSIONS},
        "n_files": args.n or "all",
        "screen_n": args.screen_n,
        "rounds": args.rounds,
        "scenarios": out,
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 打印主表 ----
    print("=" * 84)
    print(f"{'场景':<16}" + "".join(f"{lb:>16}" for lb, _ in VERSIONS))
    print("-" * 84)

    def cell(r: dict) -> str:
        if "unavailable" in r:
            return "n/a"
        if "error" in r:
            return "ERR"
        return f"{r['wall_s']:.3f}s/{r['ratio']:.2f}x"

    for name in names:
        print(f"{name:<16}" + "".join(f"{cell(out[name]['results'][lb]):>16}"
                                     for lb, _ in VERSIONS))
    print(f"\n已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
