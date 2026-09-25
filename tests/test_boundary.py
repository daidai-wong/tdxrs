# -*- coding: utf-8 -*-
"""P5 验收: Arrow / Parquet / pandas 边界互操作。

覆盖计划 §P5「交付验收」:
  ① to_arrow 值正确 —— 与 Matrix/DailyPanel 的访问器逐列逐值一致
  ② 零拷贝条件 —— 连续列进 Arrow 必须零拷贝 (地址断言);
     **默认 to_pandas() 会拷贝, split_blocks=True 才是零拷贝** (这条也要断言住)
  ③ 列存往返 —— Feather/Parquet 落盘 -> mmap 重开 -> 逐值一致
  ④ 可选依赖 —— 本模块 import 不得触发 pyarrow; 缺依赖时给可操作报错
  ⑤ 边界 —— 空记录 / 单条 / 按需建列 / 非法参数 / .lc 打包槽位 / shared_buffer 限制

用法:
    python tests/test_boundary.py                 # 黄金清单抽样
    python tests/test_boundary.py --limit 20
退出码: 0 = 全过; 1 = 有失败
preflight: 需要 pyarrow (本机装在隔离目录 D:/Agent/TDX RS/pylibs):
    PYTHONPATH="D:/Agent/TDX RS/pylibs" python tests/test_boundary.py
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).parent
GOLDEN = SCRIPT_DIR / "golden" / "golden_local.json"
VIPDOC_DEFAULT = Path(r"D:\TDX\vipdoc")
RECORD = 32

# 逐列比对时用原始整数/浮点字段, 不经过系数 (系数的正确性由 test_local_parity 覆盖)
CHECK_FIELDS = ("date", "open", "high", "low", "close", "amount", "volume")


def _golden_targets(vipdoc: Path, kind: str, limit: int) -> list[Path]:
    if not GOLDEN.exists():
        raise SystemExit(f"黄金语料不存在: {GOLDEN}\n先运行: python tests/gen_golden_local.py")
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    files = [vipdoc / f["rel"] for f in data["files"] if f["kind"] == kind]
    files = [f for f in files if f.exists()]
    return files[:limit] if limit else files


# ============================================================
# 用例
# ============================================================
def check_day_values(vipdoc: Path, limit: int, local, boundary) -> list[str]:
    """① .day: Arrow 列 vs raw 字段, 逐值比对。"""
    errs: list[str] = []
    files = _golden_targets(vipdoc, "day", limit)
    if not files:
        return ["没有 .day 样本"]
    for f in files:
        m = local.read_daily(f)
        tb = m.to_arrow(code=f.stem, date_as="date32")
        if tb.num_rows != m.n:
            errs.append(f"{f.stem}: rows {tb.num_rows} != {m.n}")
            continue
        for nm in CHECK_FIELDS:
            got = tb.column(nm).to_numpy(zero_copy_only=False)
            want = np.asarray(m.raw[nm])
            if not np.array_equal(got, want):
                bad = int(np.flatnonzero(got != want)[0]) if got.shape == want.shape else -1
                errs.append(f"{f.stem}: 列 {nm} 不一致 (首个 #{bad})")
        if str(tb.column("date32")[0]) != m.date_str[0] or \
           str(tb.column("date32")[-1]) != m.date_str[-1]:
            errs.append(f"{f.stem}: date32 首末与 date_str 不符")
        if tb.column("code")[0].as_py() != f.stem:
            errs.append(f"{f.stem}: code 列错")
    return errs


def check_lc_values(vipdoc: Path, limit: int, local, boundary) -> list[str]:
    """① .lc: 第 0 槽 date(u16)+time(u16) 打包 必须正确拆包。"""
    errs: list[str] = []
    files = _golden_targets(vipdoc, "lc", limit)
    if not files:
        return []
    for f in files:
        m = local.read_lc(f)
        tb = m.to_arrow(date_as="date32")
        if tb.num_rows != m.n:
            errs.append(f"{f.stem}: rows {tb.num_rows} != {m.n}")
            continue
        for nm in ("date", "time", "open", "high", "low", "close", "amount", "volume"):
            got = tb.column(nm).to_numpy(zero_copy_only=False)
            want = np.asarray(m.raw[nm])
            if not np.array_equal(got, want):
                errs.append(f"{f.stem}: .lc 列 {nm} 不一致 (应是打包槽拆分错误)")
        # hour/minute 与 time 的一致性
        t = tb.column("time").to_numpy(zero_copy_only=False)
        if not np.array_equal(t // 60, m.hour) or not np.array_equal(t % 60, m.minute):
            errs.append(f"{f.stem}: time -> hour/minute 不符")
        try:
            m.to_arrow(shared_buffer=True)
            errs.append(f"{f.stem}: .lc 用 shared_buffer 应报错 (槽 0 是打包值)")
        except ValueError:
            pass
    return errs


def check_panel(vipdoc: Path, limit: int, local, boundary) -> list[str]:
    """① 面板: 行数 / code 字典映射 / 与 close_grid 末列一致。"""
    errs: list[str] = []
    files = _golden_targets(vipdoc, "day", limit or 200)
    if len(files) < 2:
        return ["面板样本不足"]
    p = local.scan_daily(files)
    tb = p.to_arrow(columns=["close", "volume", "date"])
    if tb.num_rows != p.n_rows:
        errs.append(f"面板 rows {tb.num_rows} != {p.n_rows}")
    if tb.column_names != ["date", "close", "volume", "code"]:
        errs.append(f"列顺序异常: {tb.column_names}")
    # code 字典 -> 每行应等于该文件代码
    codes = np.array([str(c) for c in p.codes])
    got = np.asarray(tb.column("code").to_numpy(zero_copy_only=False))
    want = codes[p.code_indices()]
    if not np.array_equal(got, want):
        bad = int(np.flatnonzero(got != want)[0])
        errs.append(f"code 列映射错, 首个 #{bad} {got[bad]!r} != {want[bad]!r}")
    # close 列与逐文件读取一致 (抽样首尾)
    for i in (0, len(files) // 2, len(files) - 1):
        m = local.read_daily(files[i])
        seg = np.asarray(tb.column("close").to_numpy(zero_copy_only=False))[
            p.offsets[i]:p.offsets[i + 1]]
        if not np.array_equal(seg, np.asarray(m.raw["close"])):
            errs.append(f"面板 close 第 {i} 段与 read_daily 不一致")
    if not boundary.HAVE_PYARROW:
        errs.append("HAVE_PYARROW 应为 True")
    return errs


def check_roundtrip(vipdoc: Path, limit: int, local, boundary) -> list[str]:
    """③ Feather / Parquet 落盘 + mmap 重开, 逐值一致。"""
    errs: list[str] = []
    files = _golden_targets(vipdoc, "day", limit or 200)
    p = local.scan_daily(files)
    tb = p.to_arrow()
    with tempfile.TemporaryDirectory(prefix="tdxrs_boundary_") as d:
        d = Path(d)
        for ext, writer, opener in (
            (".feather", boundary.write_feather, boundary.open_feather),
            (".parquet", boundary.write_parquet, boundary.open_parquet),
        ):
            path = d / f"panel{ext}"
            t0 = time.perf_counter()
            writer(tb, path)
            w = time.perf_counter() - t0
            t0 = time.perf_counter()
            back = opener(path)
            r = time.perf_counter() - t0
            if back.num_rows != tb.num_rows:
                errs.append(f"{ext}: rows {back.num_rows} != {tb.num_rows}")
            for nm in ("date", "open", "high", "low", "close", "amount", "volume"):
                a = tb.column(nm).to_numpy(zero_copy_only=False)
                b = back.column(nm).to_numpy(zero_copy_only=False)
                if not np.array_equal(a, b):
                    errs.append(f"{ext}: 列 {nm} 往返不一致")
            ca = np.asarray(tb.column("code").to_numpy(zero_copy_only=False))
            cb = np.asarray(back.column("code").to_numpy(zero_copy_only=False))
            if not np.array_equal(ca, cb):
                errs.append(f"{ext}: code 列往返不一致")
            size_mb = path.stat().st_size / 2**20
            print(f"      {ext:9} 写 {w*1000:7.1f} ms  读 {r*1000:7.1f} ms  "
                  f"大小 {size_mb:5.1f} MB  ({tb.num_rows:,} 行)")
    return errs


def check_zero_copy(vipdoc: Path, limit: int, local, boundary) -> list[str]:
    """② 零拷贝条件 (地址断言)。"""
    import pyarrow as pa
    errs: list[str] = []
    files = _golden_targets(vipdoc, "day", limit or 200)
    p = local.scan_daily(files)

    # (a) 连续 ndarray -> pa.array 必须零拷贝
    cont = np.ascontiguousarray(p.raw2d.T)
    for i, nm in enumerate(["date", "open", "high", "low", "close",
                            "amount", "volume", "reserved"]):
        src = cont.view(np.float32)[i] if nm == "amount" else cont[i]
        if pa.array(src).buffers()[1].address != src.ctypes.data:
            errs.append(f"连续列 {nm} 进 Arrow 未零拷贝")
    # (b) 跨步列必须拷贝 —— 断言「一列为一次拷贝」而不是更多
    strided = p.raw2d[:, 4]
    arr = pa.array(strided)
    if arr.buffers()[1].address == strided.ctypes.data:
        errs.append("跨步列竟然零拷贝? 与实测结论冲突, 需重新确认")
    if arr.buffers()[1].size != strided.nbytes:
        errs.append(f"跨步列 Arrow buffer {arr.buffers()[1].size} != {strided.nbytes}")

    tb = p.to_arrow()
    addr = tb.column("close").chunk(0).buffers()[1].address
    # (c) 默认 to_pandas 会拷贝 (这是坑, 必须记录在案)
    df_default = tb.to_pandas()
    if np.asarray(df_default["close"]).ctypes.data == addr:
        errs.append("默认 to_pandas 竟然零拷贝? 实测是拷贝, 结论需更新")
    # (d) to_pandas(split_blocks=True) 是零拷贝
    df = boundary.to_pandas(tb)
    if np.asarray(df["close"]).ctypes.data != addr:
        errs.append("to_pandas(split_blocks=True) 未零拷贝")
    if str(df["code"].dtype) != "category":
        errs.append(f"code 列 dtype 应为 category, 得到 {df['code'].dtype}")
    # (e) ArrowDtype 路径
    import pandas as pd
    dfa = boundary.to_pandas(tb, arrow_backed=True)
    if np.asarray(dfa["close"]).ctypes.data != addr:
        errs.append("to_pandas(ArrowDtype) 未零拷贝")
    if not isinstance(dfa["close"].dtype, pd.ArrowDtype):
        errs.append("arrow_backed=True 未返回 ArrowDtype")
    # (f) polars from_arrow 零拷贝 (可选)
    try:
        import polars as pl
        pldf = pl.from_arrow(tb)
        if pldf.to_arrow()["close"].chunk(0).buffers()[1].address != addr:
            errs.append("polars.from_arrow 未零拷贝")
    except ImportError:
        pass
    return errs


def check_optional_dep(boundary, local) -> list[str]:
    """④ 可选依赖: 模块 import 不得触发 pyarrow, 且缺依赖报错可操作。"""
    errs: list[str] = []
    import importlib
    import subprocess
    # 子进程里屏蔽 pyarrow -> 只允许 import tdxrs.boundary / tdxrs.local 成功
    code = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'pyarrow' or name.startswith('pyarrow.'):\n"
        "            raise ImportError('blocked')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "from tdxrs import boundary, local\n"
        "assert boundary.HAVE_PYARROW is False, 'HAVE_PYARROW 应为 False'\n"
        "try:\n"
        "    boundary.matrix_to_arrow(local.Matrix.__new__(local.Matrix))\n"
        "    print('NO_ERROR')\n"
        "except ImportError as e:\n"
        "    print('IMPORTERROR' if 'pip install pyarrow' in str(e) else 'BADMSG')\n"
        "except Exception as e:\n"
        "    print('OTHER:' + type(e).__name__)\n"
    )
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    out = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""
    if out != "IMPORTERROR":
        errs.append(f"屏蔽 pyarrow 后行为异常: out={out!r} err={p.stderr.strip()[-200:]!r}")

    for mod in ("tdxrs.boundary", "tdxrs.local", "tdxrs.hybrid"):
        try:
            importlib.import_module(mod)
        except Exception as e:
            errs.append(f"import {mod} 失败: {type(e).__name__}: {e}")
    return errs


def check_edges(vipdoc: Path, local, boundary) -> list[str]:
    """⑤ 边界: 空文件 / 单条 / 按需建列 / 非法参数 / price_scale。"""
    import pyarrow as pa
    errs: list[str] = []
    with tempfile.TemporaryDirectory(prefix="tdxrs_edge_") as d:
        d = Path(d)
        # 空文件
        empty = d / "sh999999.day"
        empty.write_bytes(b"")
        m0 = local.read_daily(empty)
        tb0 = m0.to_arrow()
        if tb0.num_rows != 0:
            errs.append(f"空文件 rows {tb0.num_rows} != 0")
        # 残余字节 (非 32 倍数) -> strict 关掉后应只有整条记录
        ragged = d / "sh999998.day"
        raw = (vipdoc / "sh" / "lday" / "sh600519.day").read_bytes()
        ragged.write_bytes(raw[:64] + b"\x01\x02\x03")
        mr = local.read_daily(ragged, strict=False)
        if mr.to_arrow().num_rows != 2:
            errs.append(f"非 32 倍数 rows {mr.to_arrow().num_rows} != 2")
        # 按需建列
        m = local.read_daily(vipdoc / "sh" / "lday" / "sh600519.day")
        if m.to_arrow(columns="close").column_names != ["close"]:
            errs.append("columns='close' 未只建一列")
        # price_scale 与访问器一致
        tbs = m.to_arrow(columns=["open", "close"], price_scale=True)
        for nm in ("open", "close"):
            got = tbs.column(nm).to_numpy(zero_copy_only=False)
            want = np.asarray(getattr(m, nm))
            if not np.array_equal(got, want):
                errs.append(f"price_scale 列 {nm} 与访问器不一致")
        if not pa.types.is_float64(tbs.column("open").type):
            errs.append(f"price_scale 后类型应为 float64, 得到 {tbs.column('open').type}")
        # 非法参数
        for kw, exc in (({"columns": "nope"}, ValueError),
                        ({"date_as": "iso"}, ValueError)):
            try:
                m.to_arrow(**kw)
                errs.append(f"{kw} 未报错")
            except exc:
                pass
        # date_as 默认不额外建列
        if "date32" in m.to_arrow().column_names:
            errs.append("默认不应建 date32")
        # 与 grid 一致性: 面板 close 段末值(x0.01) == 网格末列
        # 注意 Arrow 里的 close 是**原始整数**(x100), 网格是 f64 已乘系数,
        # 两边单位不同 —— 直接比会全错 (这正是本轮踩到的坑)。
        files = sorted((vipdoc / "sh" / "lday").glob("*.day"))[:50]
        p = local.scan_daily(files, tail=30)
        tb = p.to_arrow(columns=["close"])
        raw_close = np.asarray(tb.column("close").to_numpy(zero_copy_only=False))
        g = p.close_grid()
        bad = []
        for i in range(len(files)):
            if p.counts[i] == 0:
                continue
            seg_last = float(raw_close[p.offsets[i + 1] - 1]) * p.coefficient
            if seg_last != float(g[i, -1]):
                bad.append(i)
        if bad:
            errs.append(f"close 末值(x系数)与网格不符: 段 {bad[:5]}")
    return errs


CHECKS = ("day_values", "lc_values", "panel", "roundtrip", "zero_copy",
          "optional_dep", "edges")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vipdoc", default=str(VIPDOC_DEFAULT))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    try:
        from tdxrs import boundary, local
    except ImportError as e:
        print(f"import 失败: {e}", file=sys.stderr)
        return 2
    if not boundary.HAVE_PYARROW:
        print("pyarrow 未安装 -> 跳过边界验收。装法:\n"
              '  pip install --target "D:/Agent/TDX RS/pylibs" pyarrow\n'
              '  PYTHONPATH="D:/Agent/TDX RS/pylibs" python tests/test_boundary.py',
              file=sys.stderr)
        return 2

    vipdoc = Path(args.vipdoc)
    names = [s.strip() for s in args.only.split(",")] if args.only else list(CHECKS)
    print(f"P5 边界验收: 语料 {vipdoc}, 用例 {names}")

    ok = True
    for name in names:
        t0 = time.perf_counter()
        if name == "day_values":
            errs = check_day_values(vipdoc, args.limit, local, boundary)
        elif name == "lc_values":
            errs = check_lc_values(vipdoc, args.limit, local, boundary)
        elif name == "panel":
            errs = check_panel(vipdoc, args.limit, local, boundary)
        elif name == "roundtrip":
            errs = check_roundtrip(vipdoc, args.limit, local, boundary)
        elif name == "zero_copy":
            errs = check_zero_copy(vipdoc, args.limit, local, boundary)
        elif name == "optional_dep":
            errs = check_optional_dep(boundary, local)
        elif name == "edges":
            errs = check_edges(vipdoc, local, boundary)
        else:
            print(f"未知用例 {name}")
            return 2
        dt = time.perf_counter() - t0
        ok = ok and not errs
        print(f"[{'PASS' if not errs else 'FAIL'}] {name:13} {dt:6.2f}s"
              + ("" if not errs else f"  {len(errs)} 处问题"))
        for e in errs[:10]:
            print(f"      - {e}")

    print("P5_BOUNDARY_OK" if ok else "P5_BOUNDARY_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
