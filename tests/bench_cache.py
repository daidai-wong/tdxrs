# -*- coding: utf-8 -*-
"""Arrow 列存缓存基准 —— 建一次 / 重开 / 列裁剪 / 增量 / 还原。

与 bench_local.py 同风格: 每个场景在**独立子进程**跑 (峰值内存互不污染、
缓存状态互不干扰), best-of-R, 结果落 JSON。同时跑一个 ``io_floor`` 场景
做同轮归一化 (本机 IO 地板漂移可达 5.7x, 绝对耗时单独引用没有意义)。

场景分三组:

  基线      io_floor / scan_daily            (不建缓存, 直接扫 vipdoc)
  建 + 读   build_feather / reopen_all / reopen_prune / reopen_pandas
            to_panel (逆向还原) / build_parquet / duckdb_scan
  增量      incr_noop / incr_1file / incr_all
            (在 arrow_cache/bench_src 的可写副本上做, 绝不碰真实 vipdoc)

增量三场景的意义: ``incr_1file`` 是「只有个别文件变了」(新上市/补数);
``incr_all`` 是**日线每日的常态** —— 每只股票都多一根 K 线, 所有分片都脏,
此时增量 == 全量重建, 场景会如实把 ``is_full_rebuild`` 记进结果。

用法:
    python tests/bench_cache.py --quick                  # 2000 文件抽样
    python tests/bench_cache.py                          # 全市场
    python tests/bench_cache.py --scenario incr_1file    # 单场景
    python tests/bench_cache.py --out ../bench_cache.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
REPO = SCRIPT_DIR.parent
ROOT = REPO.parent
VIPDOC_DEFAULT = Path(r"D:\TDX\vipdoc")
CACHE_ROOT = ROOT / "arrow_cache" / "bench"
SRC_COPY = ROOT / "arrow_cache" / "bench_src"
PY = sys.executable

sys.path.insert(0, str(SCRIPT_DIR))
from bench_local import peak_rss_mb, pick_files  # noqa: E402

RECORD = 32


# ============================================================
# 语料
# ============================================================
def _want_src_copy(n: int) -> list[Path]:
    """可写源副本 (只在增量场景用; 首次调用时按需复制)。"""
    SRC_COPY.mkdir(parents=True, exist_ok=True)
    have = sorted(SRC_COPY.glob("*.day"))
    if have and (not n or len(have) == n):
        return have
    shutil.rmtree(SRC_COPY, ignore_errors=True)
    SRC_COPY.mkdir(parents=True, exist_ok=True)
    for p in pick_files(VIPDOC_DEFAULT, n):
        shutil.copy2(p, SRC_COPY / p.name)
    return sorted(SRC_COPY.glob("*.day"))


def _append_one(p: Path) -> None:
    """给一个 .day 追加 1 条记录 (模拟新交易日)。"""
    import numpy as np
    last = p.stat().st_size
    with open(p, "rb") as fh:
        fh.seek(last - RECORD)
        rec = np.frombuffer(fh.read(RECORD), dtype=np.uint32).copy()
    rec[0] += 1                      # date 前进一天 (真实值无所谓, 只要变了)
    with open(p, "ab") as fh:
        fh.write(rec.tobytes())


# ============================================================
# 场景 (worker)
# ============================================================
def sc_io_floor(files, args) -> dict:
    total = 0
    for p in files:
        total += len(p.read_bytes())
    return {"rows": total // RECORD, "note": "read_bytes 全量, 逐文件释放"}


def sc_scan_daily(files, args) -> dict:
    from tdxrs import local
    p = local.scan_daily(files)
    return {"rows": p.n_rows, "note": f"{p.n_files} 文件"}


def _build(path, args, fmt="feather"):
    from tdxrs.arrow_cache import ArrowCache
    src = SRC_COPY if args.src_copy else pick_files(Path(args.vipdoc), args.n)
    shutil.rmtree(path, ignore_errors=True)   # 冷建: 不复用既有分片
    c = ArrowCache(path, fmt=fmt, shard_size=args.shard_size)
    t0 = time.perf_counter()
    info = c.build(src, workers=args.workers)
    dt = time.perf_counter() - t0
    return c, info, dt


def sc_build_feather(files, args) -> dict:
    c, info, dt = _build(CACHE_ROOT / "day_feather", args)
    return {"rows": info.n_rows,
            "info": {"build_s": round(dt, 4), "shards": info.n_shards,
                     "cache_mb": round(info.cache_bytes / 2**20, 2),
                     "source_mb": round(info.source_bytes / 2**20, 2),
                     "ratio": info.ratio, "files": info.n_files}}


def sc_build_parquet(files, args) -> dict:
    c, info, dt = _build(CACHE_ROOT / "day_parquet", args, fmt="parquet")
    return {"rows": info.n_rows,
            "info": {"build_s": round(dt, 4), "shards": info.n_shards,
                     "cache_mb": round(info.cache_bytes / 2**20, 2),
                     "ratio": info.ratio}}


def _open_and_time(path, columns, rounds):
    from tdxrs.arrow_cache import ArrowCache
    c = ArrowCache.open(path)
    best, rows = 1e9, 0
    for _ in range(rounds):
        t0 = time.perf_counter()
        tb = c.open_table(columns)
        dt = time.perf_counter() - t0
        rows = tb.num_rows
        best = min(best, dt)
    return best, rows, c


def sc_reopen_all(files, args) -> dict:
    dt, rows, c = _open_and_time(CACHE_ROOT / "day_feather", None, args.rounds)
    return {"rows": rows, "info": {"open_s": round(dt, 4),
                                   "cols": len(c.info().columns)}}


def sc_reopen_prune(files, args) -> dict:
    dt, rows, c = _open_and_time(CACHE_ROOT / "day_feather", ["close"], args.rounds)
    return {"rows": rows, "info": {"open_s": round(dt, 4), "cols": 1}}


def sc_reopen_pandas(files, args) -> dict:
    from tdxrs.arrow_cache import ArrowCache
    c = ArrowCache.open(CACHE_ROOT / "day_feather")
    best, n = 1e9, 0
    for _ in range(args.rounds):
        t0 = time.perf_counter()
        df = c.to_pandas(["code", "date", "close"])
        best = min(best, time.perf_counter() - t0)
        n = len(df)
    return {"rows": n, "info": {"open_pandas_s": round(best, 4),
                                "cols": 3}}


def sc_reopen_prune_parquet(files, args) -> dict:
    p = CACHE_ROOT / "day_parquet"
    if not p.exists():
        return {"rows": 0, "info": {"error": "缺 parquet 缓存, 先跑 build_parquet"}}
    dt, rows, _c = _open_and_time(p, ["close"], args.rounds)
    return {"rows": rows, "info": {"open_s": round(dt, 4), "cols": 1}}


def sc_duckdb_scan(files, args) -> dict:
    p = CACHE_ROOT / "day_parquet"
    if not p.exists():
        return {"rows": 0, "info": {"error": "缺 parquet 缓存, 先跑 build_parquet"}}
    try:
        import duckdb
    except ImportError:
        return {"rows": 0, "info": {"error": "duckdb 未安装"}}
    from tdxrs.arrow_cache import ArrowCache
    c = ArrowCache.open(p)
    con, name = c.duckdb(columns=["code", "date", "close"])
    best, n = 1e9, 0
    for _ in range(args.rounds):
        t0 = time.perf_counter()
        n, = con.execute(f"SELECT count(*) FROM {name}").fetchone()
        best = min(best, time.perf_counter() - t0)
    t0 = time.perf_counter()
    agg = con.execute(
        f"SELECT code, count(*), avg(close) FROM {name} GROUP BY code"
    ).fetchall()
    gy = time.perf_counter() - t0
    return {"rows": int(n),
            "info": {"duckdb_scan_s": round(best, 4),
                     "duckdb_groupby_s": round(gy, 4), "groups": len(agg)}}


def sc_to_panel(files, args) -> dict:
    from tdxrs.arrow_cache import ArrowCache
    c = ArrowCache.open(CACHE_ROOT / "day_feather")
    best, n = 1e9, 0
    for _ in range(args.rounds):
        t0 = time.perf_counter()
        panel = c.to_panel(sort=True)          # 含逆向重排 + gather
        best = min(best, time.perf_counter() - t0)
        n = panel.n_rows
    return {"rows": n, "info": {"to_panel_s": round(best, 4),
                                "note": "列式 -> 行主序逆向重排 + 排序 gather"}}


def sc_to_panel_nosort(files, args) -> dict:
    from tdxrs.arrow_cache import ArrowCache
    c = ArrowCache.open(CACHE_ROOT / "day_feather")
    best, n = 1e9, 0
    for _ in range(args.rounds):
        t0 = time.perf_counter()
        panel = c.to_panel()
        best = min(best, time.perf_counter() - t0)
        n = panel.n_rows
    return {"rows": n, "info": {"to_panel_s": round(best, 4)}}


def _incr(path, mutate, args):
    """建/读缓存 -> 变更副本 -> update(), 返回报告字段。

    必须用**目录**作为来源 (而不是文件列表), 这样 manifest 才记下可回放的来源,
    ``update()`` 能自己重新发现文件集合。
    """
    from tdxrs.arrow_cache import ArrowCache
    _want_src_copy(args.n)
    c = ArrowCache(path, shard_size=args.shard_size)
    # 缓存必须指向 SRC_COPY 这个**目录** (manifest 才有可回放的来源);
    # 目录换了/来源不可回放就重建, 否则 update() 无从发现文件集合的变化。
    if not c.exists() or c.info().source != str(SRC_COPY):
        shutil.rmtree(path, ignore_errors=True)
        c = ArrowCache(path, shard_size=args.shard_size)
        c.build(SRC_COPY, workers=args.workers)
    files = sorted(SRC_COPY.glob("*.day"))
    mutate(files)
    t0 = time.perf_counter()
    rep = c.update(workers=args.workers)
    dt = time.perf_counter() - t0
    return files, c, rep, dt


def sc_incr_noop(files, args) -> dict:
    _f, c, rep, dt = _incr(CACHE_ROOT / "incr", lambda fs: None, args)
    return {"rows": c.info().n_rows,
            "info": {"update_s": round(dt, 4), "changed": rep.n_changed,
                     "dirty_shards": len(rep.dirty_shards),
                     "reread": rep.files_reread,
                     "saved_ratio": round(rep.saved_ratio, 4),
                     "note": "无变更 -> 应当零重写"}}


def sc_incr_1file(files, args) -> dict:
    def mut(fs):
        _append_one(fs[len(fs) // 2])
    _f, c, rep, dt = _incr(CACHE_ROOT / "incr", mut, args)
    return {"rows": c.info().n_rows,
            "info": {"update_s": round(dt, 4), "changed": rep.n_changed,
                     "dirty_shards": len(rep.dirty_shards),
                     "n_shards": rep.n_shards,
                     "reread": rep.files_reread, "total_files": rep.n_files,
                     "saved_ratio": round(rep.saved_ratio, 4),
                     "full_rebuild": rep.is_full_rebuild}}


def sc_incr_all(files, args) -> dict:
    def mut(fs):
        for p in fs:
            _append_one(p)
    _f, c, rep, dt = _incr(CACHE_ROOT / "incr", mut, args)
    return {"rows": c.info().n_rows,
            "info": {"update_s": round(dt, 4), "changed": rep.n_changed,
                     "dirty_shards": len(rep.dirty_shards),
                     "n_shards": rep.n_shards,
                     "reread": rep.files_reread, "total_files": rep.n_files,
                     "saved_ratio": round(rep.saved_ratio, 4),
                     "full_rebuild": rep.is_full_rebuild,
                     "note": "日线每日常态: 每只股票多一根 K 线"}}


SCENARIOS = {
    "io_floor":           (sc_io_floor,           "IO 地板 (read_bytes 全量)"),
    "scan_daily":         (sc_scan_daily,         "基线: scan_daily 直扫 vipdoc"),
    "build_feather":      (sc_build_feather,      "建缓存: Feather (lz4)"),
    "build_parquet":      (sc_build_parquet,      "建缓存: Parquet (zstd)"),
    "reopen_all":         (sc_reopen_all,         "重开: 全列"),
    "reopen_prune":       (sc_reopen_prune,       "重开: 单列 (列裁剪)"),
    "reopen_prune_parquet": (sc_reopen_prune_parquet, "重开: Parquet 单列"),
    "reopen_pandas":      (sc_reopen_pandas,      "重开 -> DataFrame (3 列)"),
    "duckdb_scan":        (sc_duckdb_scan,        "DuckDB 直读 Parquet"),
    "to_panel":           (sc_to_panel,           "还原 DailyPanel (排序)"),
    "to_panel_nosort":    (sc_to_panel_nosort,    "还原 DailyPanel (不排序)"),
    "incr_noop":          (sc_incr_noop,          "增量: 无变更"),
    "incr_1file":         (sc_incr_1file,         "增量: 只改 1 个文件"),
    "incr_all":           (sc_incr_all,           "增量: 全部文件都变 (日线常态)"),
}
ORDER = list(SCENARIOS)


# ============================================================
# driver
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vipdoc", default=str(VIPDOC_DEFAULT))
    ap.add_argument("--n", type=int, default=None, help="样本文件数 (None=全部)")
    ap.add_argument("--quick", action="store_true", help="2000 文件快速模式")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--shard-size", type=int, default=512)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--scenario", default=None)
    ap.add_argument("--only", default=None)
    ap.add_argument("--src-copy", action="store_true",
                    help="从可写副本读 (增量场景需要)")
    ap.add_argument("--out", default=str(ROOT / "bench_cache.json"))
    args = ap.parse_args()
    if args.quick and args.n is None:
        args.n = 2000

    names = [args.scenario] if args.scenario else \
        ([s.strip() for s in args.only.split(",")] if args.only else ORDER)

    print("=" * 74)
    print(f"Arrow 缓存基准  样本 {'全部' if args.n is None else args.n} 文件"
          f"  轮数 {args.rounds}  分片 {args.shard_size} 文件/片")
    print("=" * 74)
    payload = {"scenarios": {}, "n_files": args.n, "rounds": args.rounds,
               "shard_size": args.shard_size,
               "env": _env()}
    for nm in names:
        if nm not in SCENARIOS:
            print(f"  未知场景 {nm}; 可选 {ORDER}")
            continue
        fn, desc = SCENARIOS[nm]
        env = dict(os.environ)
        env["TDXRS_BENCH_SCENARIO"] = nm
        t0 = time.perf_counter()
        p = subprocess.run([PY, str(Path(__file__).resolve()), "--worker", nm,
                            "--vipdoc", args.vipdoc,
                            "--n", str(args.n if args.n is not None else 0),
                            "--rounds", str(args.rounds),
                            "--shard-size", str(args.shard_size),
                            "--workers", str(args.workers)]
                           + (["--src-copy"] if args.src_copy or
                              nm.startswith("incr") else []),
                           capture_output=True, text=True, cwd=str(REPO), env=env,
                           encoding="utf-8", errors="replace")
        dt = time.perf_counter() - t0
        line = next((l for l in (p.stdout or "").splitlines()
                     if l.startswith("RESULT ")), None)
        if not line:
            print(f"  [FAIL] {nm:22} {(p.stderr or '').strip().splitlines()[-1:]}")
            payload["scenarios"][nm] = {"error": (p.stderr or "")[-400:]}
            continue
        r = json.loads(line[len("RESULT "):])
        r["wall_s"] = round(dt, 4)
        r["desc"] = desc
        payload["scenarios"][nm] = r
        info = r.get("info", {})
        key = next((k for k in ("build_s", "open_s", "open_pandas_s",
                                "update_s", "duckdb_scan_s", "to_panel_s")
                    if k in info), None)
        shown = f"{info[key]:.4f}s" if key else ""
        extra = info.get("note") or info.get("error") or ""
        print(f"  [{r['rows']:>10,} 行] {nm:22} {shown:>10}  "
              f"wall {r['wall_s']:6.3f}s  {extra}")

    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"\n已写入 {args.out}")
    return 0


def _env() -> dict:
    import platform
    info = {"platform": platform.platform(), "python": sys.version.split()[0]}
    try:
        import numpy
        info["numpy"] = numpy.__version__
    except Exception:  # pragma: no cover
        pass
    for m in ("pyarrow", "duckdb", "polars", "pandas"):
        try:
            info[m] = __import__(m).__version__
        except Exception:
            info[m] = "MISSING"
    return info


def worker_main(nm: str) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", default=None)
    ap.add_argument("--vipdoc", default=str(VIPDOC_DEFAULT))
    ap.add_argument("--n", type=int, default=0)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--shard-size", type=int, default=512)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--src-copy", action="store_true")
    a = ap.parse_args()
    a.n = a.n or None
    fn, _desc = SCENARIOS[nm]
    try:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        files = (_want_src_copy(a.n) if a.src_copy
                 else pick_files(Path(a.vipdoc), a.n))
        r = fn(files, a)
        if not str(r.get("note", "")).startswith("read_bytes"):
            r.setdefault("info", {})["peak_rss_mb"] = round(peak_rss_mb(), 1)
        print("RESULT " + json.dumps(r, ensure_ascii=False))
    except Exception as e:
        import traceback
        print("RESULT " + json.dumps(
            {"rows": 0, "info": {"error": f"{type(e).__name__}: {e}"},
             "trace": traceback.format_exc()[-600:]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if "--worker" in sys.argv:
        i = sys.argv.index("--worker")
        sys.exit(worker_main(sys.argv[i + 1]))
    sys.exit(main())
