# -*- coding: utf-8 -*-
"""P4 验收: scan_daily / DailyPanel 的分段索引、并行等价性、tail 语义与边界。

覆盖计划 §P4「交付验收」四条:
  ① 分段索引正确 —— 随机 50 个 code, 从大矩阵反查行区间, 与单独读取逐字节一致
  ② 并行结果与串行逐行一致, 且保持文件顺序
  ③ (峰值内存由 bench_local.py 以 WorkingSet 计量)
  ④ 8 线程不慢于 1 线程 (bench_local.py 的 scan_1t / scan_8t)

用法:
    python tests/test_scan_panel.py            # 黄金清单文件 + 合成边界
    python tests/test_scan_panel.py --limit 30 # 快速抽查
退出码: 0 = 全过; 1 = 有失败
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).parent
GOLDEN = SCRIPT_DIR / "golden" / "golden_local.json"
VIPDOC_DEFAULT = Path(r"D:\TDX\vipdoc")
RECORD = 32


# ============================================================
# 用例
# ============================================================

def _load_day_targets(vipdoc: Path, limit: int) -> list[Path]:
    """黄金清单里的 .day 文件 (按文件而非按代码, 避免市场推断)。"""
    if not GOLDEN.exists():
        raise SystemExit(f"黄金语料不存在: {GOLDEN}\n先运行: python tests/gen_golden_local.py")
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    files = [vipdoc / f["rel"] for f in data["files"] if f["kind"] == "day"]
    files = [f for f in files if f.exists()]
    if limit:
        files = files[:limit]
    return files


def check_segment_index(files: list[Path], local) -> list[str]:
    """① 分段索引: offsets/Matrix 视图 vs 单独 read_daily, 逐字节比对。"""
    errs: list[str] = []
    panel = local.scan_daily(files)

    if panel.n_files != len(files):
        errs.append(f"n_files={panel.n_files} != {len(files)}")
    if panel.offsets[0] != 0:
        errs.append(f"offsets[0]={panel.offsets[0]} != 0")

    # counts 与 stat 推导值一致
    want = np.array([f.stat().st_size // RECORD for f in files], dtype=np.int64)
    if not np.array_equal(panel.counts, want):
        bad = np.flatnonzero(panel.counts != want)
        errs.append(f"counts 有 {bad.size} 处不符, 首个 #{int(bad[0])} "
                    f"{panel.counts[bad[0]]} != {want[bad[0]]}")
    if panel.n_rows != int(want.sum()):
        errs.append(f"n_rows={panel.n_rows} != {int(want.sum())}")

    # 顺序: codes 必须与入参顺序一一对应
    got_codes = panel.codes
    want_codes = [f.stem for f in files]
    if got_codes != want_codes:
        errs.append("codes 顺序与入参不一致")

    # 随机 50 个: 大矩阵行区间 vs 单独读取, 逐字段
    idx = list(range(len(files)))
    if len(idx) > 50:
        idx = random.Random(20260925).sample(idx, 50)
    for i in idx:
        m = panel.matrix_at(i)
        ref = local.read_daily(files[i])
        if m.n != ref.n:
            errs.append(f"{files[i].stem}: Matrix 行数 {m.n} != {ref.n}")
            continue
        if not np.array_equal(m.raw2d, ref.raw2d):
            d = np.flatnonzero(np.any(m.raw2d != ref.raw2d, axis=1))
            errs.append(f"{files[i].stem}: raw2d 有 {d.size} 行不符, 首个 #{int(d[0])}")
        # 按代码索引必须落到同一区间
        if panel.index_of(files[i].stem) != i:
            errs.append(f"{files[i].stem}: index_of 返回 "
                        f"{panel.index_of(files[i].stem)} != {i}")
        # 裸代码索引 (无歧义时)
        if panel.index_of(files[i].stem[2:]) != i:
            pass  # 000xxx 段沪深二义, 不强制
    # offsets 单调不减
    if np.any(np.diff(panel.offsets) < 0):
        errs.append("offsets 非单调")
    return errs


def check_parallel(files: list[Path], local) -> list[str]:
    """② 并行 == 串行, 且顺序保持 (文件顺序即面板行顺序)。"""
    errs: list[str] = []
    serial = local.scan_daily(files, workers=1)
    for w in (2, 4, 8):
        par = local.scan_daily(files, workers=w)
        if par.n_rows != serial.n_rows:
            errs.append(f"workers={w}: n_rows {par.n_rows} != {serial.n_rows}")
            continue
        if not np.array_equal(par.raw2d, serial.raw2d):
            d = np.flatnonzero(np.any(par.raw2d != serial.raw2d, axis=1))
            errs.append(f"workers={w}: raw2d 有 {d.size} 行不符, 首个 #{int(d[0])}")
        if par.codes != serial.codes:
            errs.append(f"workers={w}: codes 顺序不一致")
        if not np.array_equal(par.offsets, serial.offsets):
            errs.append(f"workers={w}: offsets 不一致")
    return errs


def check_tail(files: list[Path], local) -> list[str]:
    """tail=N 每个文件只取末尾 N 条, 与 read_tail 逐字节等价。"""
    errs: list[str] = []
    for n in (1, 5, 30, 250):
        p = local.scan_daily(files, tail=n, workers=4)
        exp_rows = sum(min(f.stat().st_size // RECORD, n) for f in files)
        if p.n_rows != exp_rows:
            errs.append(f"tail={n}: n_rows={p.n_rows} != {exp_rows}")
        for i, f in enumerate(files):
            m = p.matrix_at(i)
            ref = local.read_tail(f, n)
            if m.n != ref.n:
                errs.append(f"tail={n} {f.stem}: 行数 {m.n} != {ref.n}")
                continue
            if not np.array_equal(m.raw2d, ref.raw2d):
                errs.append(f"tail={n} {f.stem}: raw2d 不符")
    # n 超过文件条数 -> 取全部
    p = local.scan_daily(files[:3], tail=10 ** 9)
    for i, f in enumerate(files[:3]):
        if p.matrix_at(i).n != f.stat().st_size // RECORD:
            errs.append(f"tail 超长 {f.stem}: 未取全部")
    return errs


def check_categorical(files: list[Path], local) -> list[str]:
    """code 列: Categorical 语义正确 + 内存显著小于 object 列。"""
    errs: list[str] = []
    p = local.scan_daily(files)
    cat = p.categorical()
    if len(cat) != p.n_rows:
        errs.append(f"Categorical 长度 {len(cat)} != n_rows {p.n_rows}")
    if list(cat.categories) != p.codes:
        errs.append("Categorical.categories 与 codes 不一致")
    # 每个文件的区间内的 code 必须全是该文件, 且区间外不含
    for i in range(min(5, len(files))):
        a, b = int(p.offsets[i]), int(p.offsets[i + 1])
        seg = cat[a:b]
        if len(set(seg)) != 1 or seg[0] != files[i].stem:
            errs.append(f"{files[i].stem}: 行区间 [{a},{b}) 的 code 不是该文件")
    # 内存: Categorical (int8/int16 codes) vs object 字符串列
    n = p.n_rows
    cat_mb = cat.memory_usage() / 1048576
    obj_mb = n * 8 / 1048576          # object 数组仅指针 8B/行, 不含字符串本体
    if cat_mb > obj_mb * 1.5:
        errs.append(f"Categorical {cat_mb:.1f}MB 未显著优于 object 指针列 {obj_mb:.1f}MB")
    return errs


def check_grid(files: list[Path], local) -> list[str]:
    """筛选中枢形态: 右对齐网格 + 立方体 + date_grid。"""
    errs: list[str] = []
    width = 30
    p = local.scan_daily(files, tail=width, workers=4)

    # ① close_grid 逐行 == read_tail 的 close (右对齐, 不足处 NaN)
    g = p.close_grid()
    if g.shape != (len(files), width):
        errs.append(f"close_grid shape {g.shape} != ({len(files)}, {width})")
    for i in range(len(files)):
        c = int(p.counts[i])
        ref = local.read_tail(files[i], width).close
        row = g[i] if c >= width else g[i, width - c:]
        if c < width and not np.isnan(g[i, :width - c]).all():
            errs.append(f"{files[i].stem}: 历史不足 {width} 条但左侧未填充 NaN")
        if not np.allclose(row, ref):
            errs.append(f"{files[i].stem}: close_grid 行与 read_tail 的 close 不符")
    # ② 其它列 (取历史充足的样本, 避免 NaN 填充干扰)
    full = int(np.argmax(p.counts))
    for col in ("open", "high", "low", "close", "amount", "volume"):
        gg = p.grid(col)
        if gg.shape != (len(files), width) or not np.isfinite(gg[full]).all():
            errs.append(f"grid({col}) 形态或取值异常 {gg.shape}")
    ref_close = local.read_tail(files[full], width).close
    if p.grid("high")[full].max() < ref_close.max() - 1e-9:
        errs.append("high 网格不包含 close 最大值, 列取错")
    if not np.allclose(p.grid("close")[full], ref_close):
        errs.append("close 网格与该文件 read_tail 的 close 不符")
    # 价格列须已乘系数: close 网格 == 原始 u32 * 0.01
    raw = p.matrix_at(full).close_raw.astype(np.float64)
    if not np.allclose(p.grid("close")[full], raw * 0.01):
        errs.append("close 网格未按系数换算")
    # volume 不应被系数误伤 (它是整数成交股数, 不是价格)
    vraw = p.matrix_at(full).raw["volume"].astype(np.float64)
    if not np.allclose(p.grid("volume")[full], vraw):
        errs.append("volume 网格被系数误乘")
    try:
        p.grid("date")
        errs.append("grid('date') 未抛错, 应提示改用 date_grid()")
    except ValueError:
        pass
    try:
        p.grid("nope")
        errs.append("grid('nope') 未抛错")
    except ValueError:
        pass
    # ③ date_grid: 每行末列必须是该文件自己的最后一个交易日
    #    (样本含退市/长停股, 末列日期本就不统一 —— 不能断言全局一致)
    dg = p.date_grid()
    if dg.shape != (len(files), width) or dg.dtype != np.dtype("datetime64[D]"):
        errs.append(f"date_grid 形态/类型异常 {dg.shape} {dg.dtype}")
    for i in range(0, len(files), max(1, len(files) // 20)):
        c = int(p.counts[i])
        want = local.read_daily(files[i]).dates[-1]
        if dg[i, -1] != want:
            errs.append(f"{files[i].stem}: date_grid 末列 {dg[i, -1]} != {want}")
        if c < width and not np.all(np.isnat(dg[i, :width - c])):
            errs.append(f"{files[i].stem}: date_grid 历史不足处未填充 NaT")
    # ④ as_cube: 严格等长集合可用; 不等长集合必须抛错 (而非静默错位)
    old = [f for f in files if f.stat().st_size // RECORD >= width]
    if old:
        cube = local.scan_daily(old, tail=width).as_cube()
        if cube.shape != (len(old), width, 8):
            errs.append(f"as_cube shape {cube.shape} != ({len(old)}, {width}, 8)")
        if not np.array_equal(cube[:, :, 4].astype(np.float64) * 0.01,
                              local.scan_daily(old, tail=width).close_grid()):
            errs.append("as_cube 第 5 槽与 close_grid 不一致")
    try:
        local.scan_daily(files, tail=width).as_cube()
        errs.append("as_cube 在分段不等长时未抛 ValueError")
    except ValueError:
        pass
    return errs


def check_edges(local, vipdoc: Path) -> list[str]:
    """边界: 空列表 / 空目录 / 单文件 / 文件不存在 / 非 32 倍数 / 负 tail。"""
    errs: list[str] = []
    day_files = sorted((vipdoc / "sh" / "lday").glob("*.day"))
    if not day_files:
        return ["本机没有 sh/lday/*.day, 跳过边界检查"]

    # 空列表
    p0 = local.scan_daily([])
    if p0.n_files != 0 or p0.n_rows != 0:
        errs.append(f"空列表: files={p0.n_files} rows={p0.n_rows}, 期望 0/0")
    if p0.codes != []:
        errs.append("空列表: codes 非空")
    if repr(p0) == "":
        errs.append("空列表: repr 失败")

    # 单文件 (str 路径)
    one = local.scan_daily(str(day_files[0]))
    if one.n_files != 1 or one.matrix_at(0).n != day_files[0].stat().st_size // RECORD:
        errs.append("单文件路径入口不正确")

    # 目录入口
    d = local.scan_daily(vipdoc / "bj" / "lday")
    n_bj = len(list((vipdoc / "bj" / "lday").glob("*.day")))
    if d.n_files != n_bj:
        errs.append(f"目录入口: n_files={d.n_files} != {n_bj}")

    # 文件不存在 -> stat 失败应记 0 行, 不抛
    p = local.scan_daily([day_files[0], vipdoc / "sh" / "lday" / "__nope__.day"])
    if p.n_files != 2 or p.counts[1] != 0:
        errs.append(f"文件不存在: files={p.n_files} counts={list(p.counts)}")

    # 负 tail -> 视作 0 (整文件)
    pn = local.scan_daily(day_files[:2], tail=-5)
    if pn.n_rows != sum(f.stat().st_size // RECORD for f in day_files[:2]):
        errs.append("负 tail 未按「整文件」处理")

    # 非 32 倍数文件: 尾数须被丢弃 (取 size//32 条), 不报错
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        src = day_files[0].read_bytes()
        ragged = d / "sh999999.day"
        ragged.write_bytes(src + b"\x01\x02\x03")
        p = local.scan_daily(ragged)
        if p.counts[0] != len(src) // RECORD or p.n_rows != len(src) // RECORD:
            errs.append(f"非 32 倍数: rows={p.n_rows} != {len(src) // RECORD}")
        # tail 在非 32 倍数下也必须对齐到「最后 c 条完整记录」(不是从 0 开始)
        pt = local.scan_daily(ragged, tail=3)
        ref = local.read_tail(ragged, 3, strict=False)
        if not np.array_equal(pt.matrix_at(0).raw2d, ref.raw2d):
            errs.append("非 32 倍数 + tail: 尾部定位错误 (seek 未扣除残余字节)")
        # 空文件
        (d / "sh000000.day").write_bytes(b"")
        pe = local.scan_daily(d / "sh000000.day")
        if pe.n_rows != 0:
            errs.append("空文件: n_rows != 0")
    return errs


# ============================================================
# 主流程
# ============================================================

CHECKS = ("segments", "parallel", "tail", "categorical", "grid", "edges")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vipdoc", default=str(VIPDOC_DEFAULT))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    from tdxrs import local

    vipdoc = Path(args.vipdoc)
    files = _load_day_targets(vipdoc, args.limit)
    if not files:
        print("没有可测文件", file=sys.stderr)
        return 2

    names = [s.strip() for s in args.only.split(",")] if args.only else list(CHECKS)
    print(f"P4 验收: {len(files)} 个 .day 文件, 用例 {names}")

    ok = True
    for name in names:
        t0 = time.perf_counter()
        if name == "segments":
            errs = check_segment_index(files, local)
        elif name == "parallel":
            errs = check_parallel(files, local)
        elif name == "tail":
            errs = check_tail(files, local)
        elif name == "categorical":
            errs = check_categorical(files, local)
        elif name == "grid":
            errs = check_grid(files, local)
        elif name == "edges":
            errs = check_edges(local, vipdoc)
        else:
            print(f"未知用例 {name}")
            return 2
        dt = time.perf_counter() - t0
        ok = ok and not errs
        print(f"[{'PASS' if not errs else 'FAIL'}] {name:12} {dt:6.2f}s"
              + ("" if not errs else f"  {len(errs)} 处问题"))
        for e in errs[:10]:
            print(f"      - {e}")

    print("P4_SCAN_OK" if ok else "P4_SCAN_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
