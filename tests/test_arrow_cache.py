# -*- coding: utf-8 -*-
"""Arrow 列存缓存验收 —— 分片 / 增量 / 原子性 / 无损还原。

覆盖点:
  ① build + open 往返: 每一列的值与源文件逐字节一致
  ② 列裁剪: 只存/只读部分列; 请求不存在的列必须明确报错
  ③ 增量: 只重写指纹变化的分片, **未变分片的名字与 mtime 都不变**
  ④ 空更新: 第二次 update() 必须是零重写 (内容寻址复用)
  ⑤ 全量信号: 每个文件都变了 -> is_full_rebuild=True (日线常态, 如实上报)
  ⑥ 原子性: 进程写完后不留 *.tmp-*; manifest 始终是合法 JSON
  ⑦ verify/status: 源文件增删改 + 分片丢失都能被检出
  ⑧ to_panel 无损: sort=True 时与 scan_daily 逐字节等价 (day 与 lc 两种)
  ⑨ 参数与错误路径: FileExistsError / FileNotFoundError / ValueError
 ⑩ 分片归属稳定性: 新增文件不改动既有文件的归属 (增量的前提)

语料默认**合成**(不依赖 vipdoc, CI 可跑); 传 --vipdoc 时额外用真实数据抽查。

用法:
    python tests/test_arrow_cache.py
    python tests/test_arrow_cache.py --vipdoc D:/TDX/vipdoc --limit 40
退出码: 0 = 全过; 1 = 有失败
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).parent
RECORD = 32


# ============================================================
# 合成语料: 生成与真实 vipdoc 布局完全一致的定长记录
# ============================================================
def _day_dates(n: int, start=date(2024, 1, 1)):
    return [(start + timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]


def _tdx_u16(d: date) -> int:
    """TDX u16 日期编码: year-2004 左移 11 | month*100 + day。"""
    return ((d.year - 2004) * 2048) + d.month * 100 + d.day


def make_day(path: Path, n: int, seed: int = 0):
    """写 n 条 .day 记录 (date u32 + OHLC u32 + amount f32 + volume/reserved u32)。"""
    rs = np.random.RandomState(seed)
    dts = _day_dates(n)
    rec = np.zeros(n, dtype=np.dtype([
        ("date", "<u4"), ("open", "<u4"), ("high", "<u4"), ("low", "<u4"),
        ("close", "<u4"), ("amount", "<f4"), ("volume", "<u4"),
        ("reserved", "<u4")]))
    rec["date"] = np.array([int(d) for d in dts], dtype=np.uint32)
    base = 1000 + seed * 100
    px = base + np.cumsum(rs.randint(-30, 30, size=n))
    for f in ("open", "high", "low", "close"):
        rec[f] = np.abs(px + rs.randint(-5, 5, size=n)).astype(np.uint32)
    rec["amount"] = (rs.rand(n) * 1e6).astype(np.float32)
    rec["volume"] = rs.randint(1, 10 ** 6, size=n).astype(np.uint32)
    path.write_bytes(rec.tobytes())
    return rec


def append_day(path: Path, k: int, seed: int = 99):
    """在 .day 末尾追加 k 条 (模拟每天多一根 K 线)。"""
    old = path.stat().st_size // RECORD
    dts = [(date(2024, 1, 1) + timedelta(days=i + old)).strftime("%Y%m%d")
           for i in range(k)]
    rs = np.random.RandomState(seed + old)
    rec = np.zeros(k, dtype=np.dtype([
        ("date", "<u4"), ("open", "<u4"), ("high", "<u4"), ("low", "<u4"),
        ("close", "<u4"), ("amount", "<f4"), ("volume", "<u4"),
        ("reserved", "<u4")]))
    rec["date"] = np.array([int(d) for d in dts], dtype=np.uint32)
    rec["open"] = rec["high"] = rec["low"] = rec["close"] = 1500
    rec["amount"] = np.float32(1.0)
    rec["volume"] = 7
    with open(path, "ab") as fh:
        fh.write(rec.tobytes())


def make_lc(path: Path, n: int, seed: int = 0):
    """写 n 条 .lc1 记录 (date u16 + time u16 + OHLC/amount f32 + vol/rsv u32)。"""
    rs = np.random.RandomState(seed + 500)
    d0 = date(2024, 3, 1)
    rec = np.zeros(n, dtype=np.dtype([
        ("date", "<u2"), ("time", "<u2"), ("open", "<f4"), ("high", "<f4"),
        ("low", "<f4"), ("close", "<f4"), ("amount", "<f4"),
        ("volume", "<u4"), ("reserved", "<u4")]))
    for i in range(n):
        rec["date"][i] = _tdx_u16(d0 + timedelta(days=i // 240))
        rec["time"][i] = (i % 240)
    px = 10.0 + np.cumsum(rs.randn(n) * 0.1)
    rec["open"] = px
    rec["high"] = px + 0.05
    rec["low"] = px - 0.05
    rec["close"] = px + rs.randn(n) * 0.02
    rec["amount"] = (rs.rand(n) * 1e4).astype(np.float32)
    rec["volume"] = rs.randint(1, 10 ** 5, size=n).astype(np.uint32)
    path.write_bytes(rec.tobytes())
    return rec


def make_corpus(root: Path, n_files: int = 40, kind: str = "day", n_rows: int = 120):
    root.mkdir(parents=True, exist_ok=True)
    files = []
    for i in range(n_files):
        stem = f"sh6{i:05d}"
        n = n_rows + (i % 7) * 5          # 刻意不等长 -> 覆盖真实情况
        p = root / f"{stem}.{'day' if kind == 'day' else 'lc1'}"
        make_day(p, n, seed=i) if kind == "day" else make_lc(p, n, seed=i)
        files.append(p)
    return sorted(files)


# ============================================================
# 断言工具
# ============================================================
class Checker:
    def __init__(self):
        self.errs: list[str] = []

    def ok(self, cond, msg):
        if not cond:
            self.errs.append(msg)
        return bool(cond)

    def eq(self, got, want, msg):
        return self.ok(got == want, f"{msg}: {got!r} != {want!r}")

    def raises(self, exc, fn, msg):
        try:
            fn()
        except exc:
            return True
        except Exception as e:
            self.errs.append(f"{msg}: 抛了 {type(e).__name__} 而非 {exc.__name__}")
            return False
        self.errs.append(f"{msg}: 没有抛 {exc.__name__}")
        return False


def _shard_stat(root: Path) -> dict:
    return {p.name: (int(p.stat().st_mtime_ns), p.stat().st_size)
            for p in root.glob("shard-*") if ".tmp-" not in p.name}


def _no_tmp(root: Path) -> list:
    return [p.name for p in root.rglob("*.tmp-*")]


# ============================================================
# 用例
# ============================================================
def t_build_open(c: Checker, work: Path, ArrowCache):
    """① build + open 往返 + ⑥ 原子性。"""
    src = work / "src_day"
    files = make_corpus(src, 40, "day")
    root = work / "cache_day"
    cache = ArrowCache(root, shard_size=8)
    info = cache.build(src)

    c.eq(info.n_files, 40, "n_files")
    c.eq(info.n_rows, sum(f.stat().st_size // RECORD for f in files), "n_rows")
    c.ok(info.n_shards >= 5, f"分片数应 >=5, 得到 {info.n_shards}")
    c.ok(cache.exists(), "manifest 应存在")
    c.eq(_no_tmp(root), [], "① 不应残留临时文件")
    json.loads((root / "manifest.json").read_text(encoding="utf-8"))

    tb = cache.open_table()
    c.eq(tb.num_rows, info.n_rows, "table 行数")
    c.eq(tb.column_names, ["date", "open", "high", "low", "close", "amount",
                           "volume", "reserved", "code"], "列名与顺序")

    # 逐文件比对: to_panel(sort=True) 的 codes 必须等于 scan_daily 的 codes
    from tdxrs import local
    p_ref = local.scan_daily(files)
    p_c = cache.to_panel(sort=True)
    c.eq(p_c.codes, p_ref.codes, "① codes 顺序 (sort=True 应与 scan_daily 一致)")
    c.eq(int(p_c.n_rows), int(p_ref.n_rows), "行数")
    c.ok(np.array_equal(p_c.raw2d, p_ref.raw2d), "① 行主序字节应逐字节一致")
    c.ok(np.array_equal(p_c.offsets, p_ref.offsets), "分段 offsets 应一致")
    # 反向: sort=False 时行序是分片序 (可能与 scan_daily 不同, 但必须自洽)
    p_u = cache.to_panel()
    c.eq(int(p_u.n_rows), int(p_ref.n_rows), "未排序行数")
    c.ok(sorted(p_u.codes) == sorted(p_ref.codes), "未排序 codes 集合相同")
    return cache, files, p_ref


def t_column_pruning(c: Checker, work: Path, ArrowCache):
    """② 列裁剪: 少存列 = 更小; 请求不存在的列 = 明确报错。"""
    src = work / "src_slim"
    make_corpus(src, 30, "day")
    full_root = work / "cache_full"
    ArrowCache(full_root, shard_size=8).build(src)

    root = work / "cache_slim"
    slim = ArrowCache(root, shard_size=8, columns=["date", "close", "volume"])
    info = slim.build(src)
    c.eq(list(info.columns), ["date", "close", "volume", "code"], "存了哪些列")
    c.eq(slim.open_table(["close"]).column_names, ["close"], "只读一列")
    c.eq(slim.open_table(["close", "code"]).column_names, ["close", "code"],
         "读列 + code")
    c.raises(ValueError, lambda: slim.open_table(["open"]),
             "② 读未存的列应报错")
    c.raises(ValueError, lambda: ArrowCache(work / "bad", columns=["nope"]),
             "② 列名非法应报错")
    full = ArrowCache(full_root).info()
    c.ok(info.cache_bytes < full.cache_bytes,
         f"② 裁剪后 ({info.cache_bytes}) 应小于全列 ({full.cache_bytes})")
    # 少了槽位就无法无损还原行主序矩阵
    c.raises(ValueError, lambda: ArrowCache.open(root).to_panel(),
             "② 缺槽位的缓存 to_panel 应拒绝")
    # 缺槽位也不影响只读列
    c.eq(ArrowCache.open(root).open_table(["close"]).num_rows, info.n_rows,
         "② 缺槽位仍可读已有列")
    return slim


def t_incremental(c: Checker, work: Path, ArrowCache):
    """③④⑤ 增量: 只重写脏分片 / 空更新零重写 / 全变则全量。"""
    src = work / "src_inc"
    files = make_corpus(src, 60, "day")
    root = work / "cache_inc"
    cache = ArrowCache(root, shard_size=8)
    cache.build(src)

    before = _shard_stat(root)
    target = files[10]
    append_day(target, 3)

    rep = cache.update()
    after = _shard_stat(root)

    c.eq(rep.modified, [target.stem], "③ 变更文件列表")
    c.eq(len(rep.dirty_shards), 1, "③ 只有 1 个分片脏")
    member_n = next(s["n_files"] for s in cache._manifest["shards"]
                    if int(s["id"]) == rep.dirty_shards[0])
    c.eq(rep.files_reread, member_n, "③ 重读文件数 = 该分片成员数")
    c.eq(rep.files_reread, len([
        f for f in files if cache.locate(f.stem) == cache.locate(target.stem)]),
        "③ 重读集合 = 该分片成员")
    unchanged = set(before) & set(after)
    c.ok(all(before[n] == after[n] for n in unchanged),
         "③ 未变分片的名字与 mtime 都不应变")
    c.eq(len(set(before) - set(after)), 1, "③ 恰有 1 个旧分片被替换")
    c.eq(len(set(after) - set(before)), 1, "③ 恰有 1 个新分片文件")
    c.ok(rep.saved_ratio > 0.8,
         f"③ 应省下大部分重读, 实际 {rep.saved_ratio:.2%}")
    c.eq(_no_tmp(root), [], "③ 不应残留临时文件")

    # 数据确实更新了
    from tdxrs import local
    p_ref = local.scan_daily(files)
    p_c = cache.to_panel(sort=True)
    c.ok(np.array_equal(p_c.raw2d, p_ref.raw2d), "③ 增量后数据应与重扫一致")

    # ④ 空更新: 零重写 + 内容寻址全部复用
    rep2 = cache.update()
    c.eq(rep2.n_changed, 0, "④ 无变更文件")
    c.eq(rep2.dirty_shards, [], "④ 无脏分片")
    c.eq(rep2.shards_written + rep2.shards_reused, 0, "④ 不应有任何分片动作")
    c.ok(cache.status()["fresh"], "④ 更新后应 fresh")

    # ⑤ 全部文件都变 -> 全量重建信号
    for f in files:
        append_day(f, 1)
    rep3 = cache.update()
    c.ok(rep3.is_full_rebuild, "⑤ 全变应报告 is_full_rebuild")
    c.eq(len(rep3.dirty_shards), cache.info().n_shards, "⑤ 全部分片都脏")
    c.ok(rep3.saved_ratio == 0.0, "⑤ 全量时节省比例为 0")
    return cache, files


def t_added_removed(c: Checker, work: Path, ArrowCache):
    """③ 源文件增删都能被正确识别并收敛。"""
    src = work / "src_ar"
    files = make_corpus(src, 30, "day")
    root = work / "cache_ar"
    cache = ArrowCache(root, shard_size=8)
    cache.build(src)
    n_before = cache.info().n_files

    newp = src / "sh699999.day"
    make_day(newp, 50, seed=777)
    gone = files[0]
    gone.unlink()

    c.ok(not cache.status()["fresh"], "增删后 status 应不 fresh")
    d = cache.stale_files()
    c.eq(d["added"], ["sh699999"], "added")
    c.eq(d["removed"], [gone.stem], "removed")

    rep = cache.update()
    c.eq(sorted(rep.added), ["sh699999"], "update added")
    c.eq(rep.removed, [gone.stem], "update removed")
    c.eq(cache.info().n_files, n_before, "文件数: 加一个减一个应持平")
    c.ok(cache.status()["fresh"], "更新后应 fresh")
    c.eq(_no_tmp(root), [], "不应残留临时文件")
    # 被删文件的 code 不应再出现
    codes = cache.open_table(["code"])["code"].to_pylist()
    c.ok(gone.stem not in codes, "③ 已删文件的 code 不应残留")


def t_verify(c: Checker, work: Path, ArrowCache):
    """⑦ verify / status: 源变更 + 分片丢失都能检出。"""
    src = work / "src_vf"
    files = make_corpus(src, 24, "day")
    root = work / "cache_vf"
    cache = ArrowCache(root, shard_size=8)
    cache.build(src)

    v = cache.verify()
    c.ok(v.ok and not v.stale, f"刚建完应 ok, 得到 {v!r}")
    c.eq(v.n_checked, 24, "校验文件数")

    append_day(files[3], 2)
    files[5].unlink()
    v = cache.verify()
    c.ok(not v.ok or v.stale, "变更后应报不干净")
    c.eq(v.modified_files, [files[3].stem], "verify modified")
    c.eq(v.missing_files, [files[5].stem], "verify missing")

    # 分片丢失
    sh = cache.shard_paths()[0]
    sh.unlink()
    v = cache.verify()
    c.ok(not v.ok, "分片丢失应 ok=False")
    c.ok(len(v.missing_shards) == 1, f"应报 1 个分片丢失: {v.missing_shards}")
    c.raises(FileNotFoundError, lambda: cache.open_table(),
             "⑦ 分片丢失时读取应报错")

    # 重建后恢复
    rep = cache.update()
    c.ok(rep.shards_written >= 1, "⑦ 分片丢失应触发重写")
    c.ok(cache.verify().ok, "⑦ 重建后应恢复")


def t_errors(c: Checker, work: Path, ArrowCache):
    """⑨ 参数与错误路径。"""
    src = work / "src_err"
    make_corpus(src, 20, "day")
    root = work / "cache_err"
    cache = ArrowCache(root, shard_size=8)
    cache.build(src)
    c.raises(FileExistsError, lambda: cache.build(src), "⑨ 重复 build 应报错")
    c.ok(cache.build(src, force=True).n_files == 20, "⑨ force=True 应重建")

    c.raises(FileNotFoundError,
             lambda: ArrowCache.open(work / "nowhere"), "⑨ 打开不存在目录")
    c.raises(ValueError, lambda: ArrowCache(work / "x", kind="week"),
             "⑨ 非法 kind")
    c.raises(ValueError, lambda: ArrowCache(work / "x", fmt="csv"),
             "⑨ 非法 format")
    c.raises(ValueError, lambda: ArrowCache(work / "x", date_as="str"),
             "⑨ 非法 date_as")
    c.raises(ValueError, lambda: ArrowCache(work / "x", columns=[]),
             "⑨ 空列集合")

    # schema 不符
    mp = root / "manifest.json"
    m = json.loads(mp.read_text(encoding="utf-8"))
    m["schema"] = 999
    mp.write_text(json.dumps(m), encoding="utf-8")
    c.raises(ValueError, lambda: ArrowCache.open(root), "⑨ schema 不符应报错")

    # 重复 stem (两个市场同名文件被塞进同一个缓存)
    dup = work / "src_dup"
    (dup / "a").mkdir(parents=True, exist_ok=True)
    (dup / "b").mkdir(parents=True, exist_ok=True)
    make_day(dup / "a" / "sh600000.day", 10, 1)
    make_day(dup / "b" / "sh600000.day", 10, 2)
    c.raises(ValueError, lambda: ArrowCache(work / "cache_dup").build(
        [dup / "a" / "sh600000.day", dup / "b" / "sh600000.day"]),
        "⑨ stem 冲突应报错")

    # 未构建就 update
    c.raises(FileNotFoundError,
             lambda: ArrowCache(work / "cache_none").update(),
             "⑨ 未构建的缓存 update 应报错")

    # 构建时传**文件列表**: 来源无法回放, 必须明确表达而不是静默把全部文件报成变更
    lst = ArrowCache(work / "cache_list", shard_size=8)
    lst.build(sorted(src.glob("*.day")))
    c.eq(lst.info().source, "", "⑨ 列表来源不应被写成路径字符串")
    c.ok(lst.status()["fresh"] is None,
         f"⑨ 来源不可知时 fresh 应为 None, 得到 {lst.status()['fresh']!r}")
    c.eq(lst.stale_files()["modified"], [],
         "⑨ 来源不可知时不应把全部文件报成 modified")
    v = lst.verify()
    c.ok(v.ok and not v.source_comparable, "⑨ 来源不可知时 verify 应标注不可比")
    c.raises(ValueError, lambda: lst.update(),
             "⑨ 来源不可知时 update 应要求显式 source")
    c.eq(lst.update(source=src).n_changed, 0, "⑨ 显式 source 后 update 应可用")

    # clear
    c2 = ArrowCache(work / "cache_clr", shard_size=8)
    c2.build(src)
    c2.clear()
    c.ok(not c2.exists(), "⑨ clear 后不应存在")
    c2.build(src)
    c.ok(c2.exists(), "⑨ clear 后可重建")


def t_shard_stability(c: Checker, work: Path, ArrowCache, mod):
    """⑩ 分片归属稳定性 —— 增量的前提条件。"""
    n = 12
    stems = [f"sh6{i:05d}" for i in range(200)]
    a = {s: mod._shard_of(s, n) for s in stems}
    # 同一 stem 反复计算必须一致 (与调用顺序无关)
    b = {s: mod._shard_of(s, n) for s in reversed(stems)}
    c.eq(a, b, "⑩ 分片归属必须确定")
    # 新增文件不影响既有文件的归属
    a2 = {s: mod._shard_of(s, n) for s in stems + ["sz000001", "bj430047"]}
    c.ok(all(a2[s] == a[s] for s in stems), "⑩ 新增文件不得改变既有归属")
    # n_shards 变化会重排 -> 所以 manifest 定下后不能改
    c.ok(any(mod._shard_of(s, 7) != a[s] for s in stems),
         "⑩ n_shards 变化应导致重排 (故 manifest 固定 n_shards)")
    # 分布不应严重倾斜
    from collections import Counter
    cnt = Counter(a.values())
    c.ok(max(cnt.values()) < len(stems) * 0.5,
         f"⑩ 分布不应严重倾斜: {sorted(cnt.values())}")


def t_lc(c: Checker, work: Path, ArrowCache):
    """⑧ .lc 通道: 槽 0 的 date+time 打包必须无损还原。"""
    src = work / "src_lc"
    files = make_corpus(src, 12, "lc", n_rows=240)
    root = work / "cache_lc"
    cache = ArrowCache(root, kind="lc", shard_size=4)
    info = cache.build(src)
    c.eq(info.kind, "lc", "kind")
    c.eq(sorted(info.columns),
         sorted(["date", "time", "open", "high", "low", "close", "amount",
                 "volume", "reserved", "code"]), "⑧ lc 列集合")

    tb = cache.open_table(["date", "time"])
    c.ok(tb["date"].to_numpy().max() > 0, "⑧ date 应已从打包槽拆出")

    from tdxrs import local
    p_ref = local.scan_lc(files)
    p_c = cache.to_panel(sort=True)
    c.eq(p_c.codes, p_ref.codes, "⑧ lc codes")
    c.ok(np.array_equal(p_c.raw2d, p_ref.raw2d), "⑧ lc 行主序字节一致")
    # 拆出来的 date/time 语义也要对
    m_ref = p_ref.matrix_at(0)
    c.ok(np.array_equal(p_c.matrix_at(0).datetimes, m_ref.datetimes),
         "⑧ lc datetimes 应一致")
    return cache


def t_pandas_duckdb(c: Checker, work: Path, ArrowCache):
    """消费端: pandas 零拷贝 + DuckDB 直读 parquet (可选依赖, 缺则 SKIP)。"""
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        c.errs.append("pyarrow 未安装, 无法验证消费端")
        return
    src = work / "src_pq"
    files = make_corpus(src, 20, "day")
    root = work / "cache_pq"
    cache = ArrowCache(root, fmt="parquet", shard_size=8)
    cache.build(src)
    c.ok(all(p.suffix == ".parquet" for p in cache.shard_paths()),
         "parquet 分片后缀")
    tb = cache.open_table(["close", "volume"])
    c.eq(tb.column_names, ["close", "volume"], "parquet 列裁剪")
    df = cache.to_pandas(["code", "date", "close"])
    c.eq(len(df), cache.info().n_rows, "pandas 行数")
    d = cache.stale_files()
    c.eq(d["modified"], [], "parquet 缓存应 fresh")

    try:
        import duckdb  # noqa: F401
    except ImportError:
        print("      (duckdb 未安装 -> 跳过直读检查)")
        return
    con, name = cache.duckdb(columns=["close"])
    n, mx = con.execute(f"SELECT count(*), max(close) FROM {name}").fetchone()
    c.eq(int(n), cache.info().n_rows, "duckdb 行数")
    c.ok(mx is not None, "duckdb 聚合")


def t_vipdoc(c: Checker, work: Path, ArrowCache, vipdoc: Path, limit: int):
    """真实 vipdoc 抽查 (先把文件**复制**到临时目录, 绝不改动真实数据)。"""
    from tdxrs import local
    d = vipdoc / "sh" / "lday"
    if not d.is_dir():
        d = vipdoc / "sz" / "lday"
    src_files = sorted(d.glob("*.day"))[:limit]
    if not src_files:
        print("      (真实 vipdoc 无 .day, 跳过)")
        return
    src = work / "src_real"
    src.mkdir(parents=True, exist_ok=True)
    for p in src_files:
        shutil.copy2(p, src / p.name)
    files = sorted(src.glob("*.day"))

    root = work / "cache_real"
    cache = ArrowCache(root, shard_size=8)
    info = cache.build(src)
    c.eq(info.n_files, len(files), "真实语料文件数")
    p_ref = local.scan_daily(files)
    p_c = cache.to_panel(sort=True)
    c.ok(np.array_equal(p_c.raw2d, p_ref.raw2d),
         f"真实语料 {len(files)} 文件逐字节一致")
    c.eq(p_c.codes, p_ref.codes, "真实语料 codes 顺序")
    # 增量: 改一个副本
    before = _shard_stat(root)
    append_day(files[0], 1)
    rep = cache.update()
    after = _shard_stat(root)
    c.eq(len(rep.dirty_shards), 1, "真实语料增量只应脏 1 个分片")
    c.ok(all(before[n] == after[n] for n in set(before) & set(after)),
         "真实语料未变分片不应被触碰")
    c.ok(np.array_equal(cache.to_panel(sort=True).raw2d,
                        local.scan_daily(files).raw2d), "增量后仍一致")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vipdoc", default=r"D:\TDX\vipdoc")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    try:
        from tdxrs.arrow_cache import ArrowCache
        from tdxrs import arrow_cache as mod
    except ImportError as e:
        print(f"无法导入 tdxrs.arrow_cache: {e}")
        return 1
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        print("pyarrow 未安装 -> 跳过 Arrow 缓存验收。装法:\n"
              '  pip install --target "D:/Agent/TDX RS/pylibs" pyarrow\n'
              '  PYTHONPATH="D:/Agent/TDX RS/pylibs" python tests/test_arrow_cache.py')
        return 2  # 与 test_boundary 对齐: 缺可选依赖 = SKIP(退出码 2), 而非假绿 PASS

    cases = {
        "build_open": lambda c, w: t_build_open(c, w, ArrowCache),
        "pruning": lambda c, w: t_column_pruning(c, w, ArrowCache),
        "incremental": lambda c, w: t_incremental(c, w, ArrowCache),
        "added_removed": lambda c, w: t_added_removed(c, w, ArrowCache),
        "verify": lambda c, w: t_verify(c, w, ArrowCache),
        "errors": lambda c, w: t_errors(c, w, ArrowCache),
        "shard_stability": lambda c, w: t_shard_stability(c, w, ArrowCache, mod),
        "lc": lambda c, w: t_lc(c, w, ArrowCache),
        "pandas_duckdb": lambda c, w: t_pandas_duckdb(c, w, ArrowCache),
        "vipdoc": lambda c, w: t_vipdoc(c, w, ArrowCache, Path(args.vipdoc),
                                        args.limit),
    }
    names = [args.only] if args.only else list(cases.keys())
    print(f"Arrow 缓存验收: {len(names)} 类用例")
    tmp = tempfile.mkdtemp(prefix="tdxrs_cache_")
    t_all = time.perf_counter()
    total = 0
    try:
        for nm in names:
            c = Checker()
            w = Path(tmp) / nm
            w.mkdir(parents=True, exist_ok=True)
            t0 = time.perf_counter()
            try:
                cases[nm](c, w)
            except Exception as e:
                import traceback
                c.errs.append(f"异常: {type(e).__name__}: {e}\n"
                              + "".join(traceback.format_exc().splitlines(True)[-4:]))
            dt = time.perf_counter() - t0
            status = "PASS" if not c.errs else "FAIL"
            print(f"  [{status}] {nm:16} {dt:5.2f}s"
                  + ("" if not c.errs else f"  ({len(c.errs)} 处)"))
            for e in c.errs:
                print(f"        - {e}")
            total += len(c.errs)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"合计: {total} 处问题, 用时 {time.perf_counter() - t_all:.2f}s")
    if total:
        print("ARROW_CACHE_FAIL")
        return 1
    print("ARROW_CACHE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
