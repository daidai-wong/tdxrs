# -*- coding: utf-8 -*-
"""本地解析基准测试单入口 -- 替代散落的 probe_*.py / profile_local_parse.py / batch_assemble.py。

设计要点:
  * 每个场景在**独立子进程**中运行 -> 内存峰值互不污染, 结果可单独复现。
  * 每个场景 best-of-R 轮取最优 -> 消除冷缓存/杀软扫描的顺序偏差。
  * **每次都测同轮 IO 地板** -> 所有耗时同时给出绝对值与 `耗时 / IO 地板` 归一化比值。
    这台机器的 IO 地板会在 0.889s ~ 4.710s 之间漂移(5.3x), 只报绝对值没有意义。
  * 未实现的场景自动 SKIP(如 P2 之前没有 read_daily), 不报错。

用法:
    python tests/bench_local.py --quick                 # 2000 文件快速体检
    python tests/bench_local.py                         # 全市场 9233 文件
    python tests/bench_local.py --out before.json       # 落盘供前后对比
    python tests/bench_local.py --scenario io_floor     # 单场景调试
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
VIPDOC_DEFAULT = Path(r"D:\TDX\vipdoc")
PY = sys.executable


# ============================================================
# 内存 (必须显式设 argtypes/restype, 否则 Windows 上静默返回 0)
# ============================================================
class _PMC(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _bind_mem() -> None:
    for dll, fname in (("psapi", "GetProcessMemoryInfo"), ("kernel32", "K32GetProcessMemoryInfo")):
        try:
            fn = getattr(getattr(ctypes.windll, dll), fname)
            fn.argtypes = [wt.HANDLE, ctypes.POINTER(_PMC), wt.DWORD]
            fn.restype = wt.BOOL
        except Exception:
            pass


_bind_mem()


def peak_rss_mb() -> float:
    c = _PMC(); c.cb = ctypes.sizeof(c)
    fn = ctypes.windll.kernel32.K32GetProcessMemoryInfo
    fn(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return c.PeakWorkingSetSize / 1048576


# ============================================================
# 语料选择 (worker 与 driver 必须一致 -> 纯函数)
# ============================================================
def pick_files(vipdoc: Path, n: int | None, min_size: int = 0) -> list[Path]:
    files = []
    for mkt in ("sh", "sz", "bj"):
        d = vipdoc / mkt / "lday"
        if d.exists():
            files += sorted(d.glob("*.day"))
    if min_size:
        files = [f for f in files if f.stat().st_size >= min_size]
    if n and len(files) > n:
        files = files[:: max(1, len(files) // n)][:n]
    return files


def pick_screen_files(vipdoc: Path, n: int = 300, min_size: int = 0) -> list[Path]:
    """筛查场景样本: 全市场均匀抽样 (贴近真实选股) 。

    注意本机 vipdoc 只保留近年数据 (.day 均值 ~34KB / 中位数 ~39KB / 最大 ~265KB),
    因此 >=48KB 的长历史样本仅 88 个 -- 若用 size 阈值会严重偏斜。
    默认不加 size 过滤, 全市场均匀抽样。

    返回 Path 而非 code: 文件名形如 `sh600519.day`, 按 stem 判断市场会全部落错分支。
    """
    files = pick_files(vipdoc, None, min_size=min_size)
    if len(files) > n:
        files = files[:: max(1, len(files) // n)][:n]
    return files


# ============================================================
# 场景实现 (worker 内运行)
# ============================================================
def sc_io_floor(files: list[Path], vipdoc: Path) -> dict:
    total = 0
    for p in files:
        total += len(p.read_bytes())      # 及时释放, 不让 buffer 堆积
    return {"rows": total // 32, "note": "read_bytes 全量, 逐文件释放"}


def sc_df_1t(files: list[Path], vipdoc: Path) -> dict:
    from tdxrs._internal import DailyBarReader
    R = DailyBarReader()
    n = 0
    for p in files:
        n += len(R.to_dataframe_file(str(p)))
    return {"rows": n}


def sc_df_8t(files: list[Path], vipdoc: Path) -> dict:
    from concurrent.futures import ThreadPoolExecutor
    from tdxrs._internal import DailyBarReader
    R = DailyBarReader()
    with ThreadPoolExecutor(8) as ex:
        n = sum(ex.map(lambda p: len(R.to_dataframe_file(str(p))), files))
    return {"rows": n, "threads": 8}


def sc_tuples_1t(files: list[Path], vipdoc: Path) -> dict:
    from tdxrs._internal import DailyBarReader
    R = DailyBarReader()
    n = 0
    for p in files:
        n += len(R.parse_file_tuples(str(p)))
    return {"rows": n}


def _have(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def sc_zc_1t(files: list[Path], vipdoc: Path) -> dict:
    from tdxrs import local
    n = 0
    for p in files:
        n += local.read_daily(p).n
    return {"rows": n}


def sc_zc_8t(files: list[Path], vipdoc: Path) -> dict:
    from concurrent.futures import ThreadPoolExecutor
    from tdxrs import local
    with ThreadPoolExecutor(8) as ex:
        n = sum(ex.map(lambda p: local.read_daily(p).n, files))
    return {"rows": n, "threads": 8}


def sc_mmap_1t(files: list[Path], vipdoc: Path) -> dict:
    from tdxrs import local
    n = 0
    for p in files:
        with local.read_daily_mmap(p) as m:
            n += m.n
    return {"rows": n, "note": "np.memmap, 0 次显式拷贝"}


def sc_zc_batch(files: list[Path], vipdoc: Path) -> dict:
    """零拷贝 + concat 拼装(批量路径)。"""
    import numpy as np
    from tdxrs import local
    parts = []
    for p in files:
        parts.append(local.read_daily(p).raw)
    v = np.concatenate(parts)
    return {"rows": v.shape[0]}


def sc_scan_daily(files: list[Path], vipdoc: Path) -> dict:
    from tdxrs import local
    res = local.scan_daily(files)
    return {"rows": int(res.n), "codes": len(files)}


def _screen_target(f: Path) -> tuple[int, str]:
    """文件路径 -> (market, code)。直接由目录决定市场, 不走代码前缀推断。

    000xxx 段沪深二义 (000001 既是上证指数也是平安银行), 靠前缀会把
    sh000001 读成 sz000001, 导致两类筛查场景读的不是同一批文件、条数不可比。
    """
    from tdxrs.constants import MARKET_BJ, MARKET_SH, MARKET_SZ
    mkt = {"sh": MARKET_SH, "sz": MARKET_SZ, "bj": MARKET_BJ}[f.parent.parent.name]
    return mkt, f.stem[2:]


def sc_screen_legacy(files: list[Path], vipdoc: Path) -> dict:
    """改造前的行为锚点: 全量解析 -> list[dict] -> 切片。

    必须在基准里保留这条路径 —— 一旦 HybridClient 改走快路径,
    就再也测不到「旧行为有多慢」, 前后对比会失去基准。
    """
    from tdxrs.hybrid import read_local_day
    n = 0
    for f in files:
        n += len(read_local_day(f)[-30:])
    return {"rows": n, "codes": len(files), "note": "旧逻辑: 全量解析+切片"}


def sc_screen_hybrid(files: list[Path], vipdoc: Path) -> dict:
    """改造后的 HybridClient (dict 输出形态保持不变)。"""
    from tdxrs.hybrid import HybridClient
    hc = HybridClient()
    n = 0
    for f in files:
        mkt, code = _screen_target(f)
        r = hc.get_daily_bars(code, count=30, market=mkt, persist=False, validate=False)
        n += len(r["bars"])
    return {"rows": n, "codes": len(files), "note": "P3 快路径, 输出仍为 list[dict]"}


def sc_screen_tail(files: list[Path], vipdoc: Path) -> dict:
    from tdxrs import local
    n = 0
    for f in files:
        n += local.read_tail(f, 30).n
    return {"rows": n, "codes": len(files), "note": "只读尾部 30x32 字节"}


SCENARIOS = {
    "io_floor":       (sc_io_floor,       "IO 地板: read_bytes 全量"),
    "tuples_1t":      (sc_tuples_1t,      "现状 list[tuple] 1 线程"),
    "df_1t":          (sc_df_1t,          "现状 to_dataframe_file 1 线程"),
    "df_8t":          (sc_df_8t,          "现状 to_dataframe_file 8 线程"),
    "zc_1t":          (sc_zc_1t,          "零拷贝 read_daily 1 线程"),
    "zc_8t":          (sc_zc_8t,          "零拷贝 read_daily 8 线程"),
    "mmap_1t":        (sc_mmap_1t,        "零拷贝 np.memmap 1 线程"),
    "zc_batch":       (sc_zc_batch,       "零拷贝 + concat 批量拼装"),
    "scan_daily":     (sc_scan_daily,     "批量并行扫描 scan_daily"),
    "screen_legacy":  (sc_screen_legacy,  "筛查(旧行为锚点): 全量解析+切片"),
    "screen_hybrid":  (sc_screen_hybrid,  "筛查: HybridClient(count=30)"),
    "screen_tail":    (sc_screen_tail,    "筛查: read_tail(30)"),
}

# 需要 tdxrs.local 的场景(P2 之后才有)
NEEDS_LOCAL = {"zc_1t", "zc_8t", "mmap_1t", "zc_batch", "scan_daily", "screen_tail"}
# 用筛查样本(300 长历史文件)而非全市场样本的场景
SCREEN_SCENARIOS = {"screen_legacy", "screen_hybrid", "screen_tail"}


# ============================================================
# worker
# ============================================================
def run_worker(args) -> int:
    vipdoc = Path(args.vipdoc)
    if args.scenario in SCREEN_SCENARIOS:
        files = pick_screen_files(vipdoc, args.screen_n)
    else:
        files = pick_files(vipdoc, args.n)

    fn, _desc = SCENARIOS[args.scenario]
    if args.scenario in NEEDS_LOCAL and not _have("tdxrs.local"):
        print("RESULT " + json.dumps({"scenario": args.scenario, "skipped": "tdxrs.local 未实现"}))
        return 0

    rounds = []
    info: dict = {}
    for r in range(args.rounds):
        t0 = time.perf_counter()
        info = fn(files, vipdoc)
        rounds.append(time.perf_counter() - t0)

    print("RESULT " + json.dumps({
        "scenario": args.scenario,
        "files": len(files),
        "rows": info.get("rows", 0),
        "info": {k: v for k, v in info.items() if k != "rows"},
        "wall_s": round(min(rounds), 6),
        "rounds": [round(x, 6) for x in rounds],
        "peak_rss_mb": round(peak_rss_mb(), 1),
    }))
    return 0


# ============================================================
# driver
# ============================================================
def env_info() -> dict:
    import platform

    def _v(mod):
        try:
            return getattr(__import__(mod), "__version__", "?")
        except Exception:
            return "MISSING"

    return {
        "platform": platform.platform(),
        "cpu": platform.processor() or "?",
        "python": sys.version.split()[0],
        "numpy": _v("numpy"),
        "pandas": _v("pandas"),
        "pyarrow": _v("pyarrow"),
        "cwd": str(SCRIPT_DIR.parent),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vipdoc", default=str(VIPDOC_DEFAULT))
    ap.add_argument("--n", type=int, default=None, help="全市场样本数 (None=全部)")
    ap.add_argument("--screen-n", type=int, default=300, help="筛查场景样本数")
    ap.add_argument("--rounds", type=int, default=3, help="每场景轮数, 取最优")
    ap.add_argument("--quick", action="store_true", help="2000 文件快速模式")
    ap.add_argument("--scenario", default=None)
    ap.add_argument("--out", default=None, help="结果 JSON 路径")
    ap.add_argument("--only", default=None, help="逗号分隔的场景名")
    args = ap.parse_args()

    if args.quick and args.n is None:
        args.n = 2000

    if args.scenario:
        return run_worker(args)

    names = list(SCENARIOS)
    if args.only:
        names = [s.strip() for s in args.only.split(",") if s.strip() in SCENARIOS]

    env = env_info()
    print("=" * 78)
    print(f"本地解析基准  {env['platform']}")
    print(f"python {env['python']}  numpy {env['numpy']}  pandas {env['pandas']}  pyarrow {env['pyarrow']}")
    print("=" * 78)

    results: dict[str, dict] = {}
    for name in names:
        cmd = [PY, str(Path(__file__)), "--vipdoc", args.vipdoc, "--scenario", name,
               "--rounds", str(args.rounds), "--screen-n", str(args.screen_n)]
        if args.n:
            cmd += ["--n", str(args.n)]
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(SCRIPT_DIR.parent))
        line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT ")), None)
        if not line:
            err = (p.stderr.strip().splitlines() or ["?"])[-1]
            print(f"  {name:15} FAILED: {err}")
            results[name] = {"error": p.stderr[-500:]}
            continue
        r = json.loads(line[7:])
        results[name] = r
        if "skipped" in r:
            print(f"  {name:15} SKIP   ({r['skipped']})")
        else:
            print(f"  {name:15} {r['wall_s']:8.3f}s  {r['rows']:>10,} 条  "
                  f"峰值 {r['peak_rss_mb']:7.1f} MB")

    floor = results.get("io_floor", {}).get("wall_s")
    if floor:
        print("-" * 78)
        print(f"同轮 IO 地板: {floor:.3f}s  ->  归一化比值 (>1 表示慢于纯 IO)")
        base = results.get("df_1t", {}).get("wall_s")
        for name, r in results.items():
            if "wall_s" not in r:
                continue
            ratio = r["wall_s"] / floor
            extra = ""
            if base and name not in ("io_floor", "df_1t"):
                extra = f"   vs df_1t {base / r['wall_s']:.2f}x"
            print(f"  {name:15} /IO = {ratio:6.2f}x{extra}")

    payload = {
        "env": env,
        "n_files": args.n or "all",
        "screen_n": args.screen_n,
        "rounds": args.rounds,
        "io_floor_s": floor,
        "scenarios": results,
    }
    out = args.out or str(SCRIPT_DIR.parent / "bench_local.json")
    Path(out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
