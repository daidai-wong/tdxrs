# -*- coding: utf-8 -*-
"""P3 验收: 按需区间读取 + HybridClient 快路径的等价性。

判据: 新路径的输出必须与「旧逻辑」逐字段 / 逐键相同。
      旧逻辑 = 全量解析后再切片 (read_local_day(path)[-count:])。

覆盖:
  1. read_tail 与全量尾部切片一致 (多档 n, 含 n > 文件条数 / n = 0)
  2. read_range 与全量按日期过滤一致 (多个区间分位)
  3. HybridClient.get_daily_bars 快路径与旧逻辑逐键相同 (含 source / local_count)
  4. 边界: count=0 (旧行为是返回全部)、count>文件条数、空文件、非 32 倍数、文件不存在

用法: python tests/test_tail_range.py [--all]
退出码: 0 = 全部通过
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).parent
GOLDEN = SCRIPT_DIR / "golden" / "golden_local.json"
VIPDOC_DEFAULT = Path(r"D:\TDX\vipdoc")

TAIL_NS = [1, 2, 5, 30, 250, 800]
RANGE_FRACS = [(0.0, 1.0), (0.0, 0.0), (0.25, 0.75), (0.9, 1.0), (0.5, 0.52)]


class _NoServer:
    """注入的空服务器客户端: 让慢路径拿不到网络数据, 只回本地。"""

    def get_security_bars(self, *a, **k):
        return []


def _eq_arrays(a, b) -> bool:
    return np.array_equal(a, b)


def check_tail(vipdoc: Path, rel: str) -> list[str]:
    from tdxrs.local import read_daily, read_tail

    path = vipdoc / rel
    m = read_daily(path)
    errs = []
    total = m.n

    for n in TAIL_NS + [total - 1, total, total + 1, total * 2, 0]:
        if n < 0:
            continue
        t = read_tail(path, n)
        want = max(0, min(n, total))
        if t.n != want:
            errs.append(f"{rel} n={n}: 条数 {t.n} != {want}")
            continue
        if want == 0:
            continue
        ref = m.tail(want)
        if ref.date_str != t.date_str:
            errs.append(f"{rel} n={n}: 日期不一致")
        for f in ("open", "high", "low", "close", "amount", "volume"):
            if not _eq_arrays(getattr(ref, f), getattr(t, f)):
                errs.append(f"{rel} n={n}: {f} 不一致")
    return errs


def check_range(vipdoc: Path, rel: str) -> list[str]:
    from tdxrs.local import read_daily, read_range

    path = vipdoc / rel
    m = read_daily(path)
    errs = []
    total = m.n
    if total == 0:
        return errs
    ds = m.date_str

    for f0, f1 in RANGE_FRACS:
        i0 = min(int(f0 * (total - 1)), total - 1)
        i1 = min(int(f1 * (total - 1)), total - 1)
        d0, d1 = ds[i0], ds[i1]
        r = read_range(path, d0, d1)
        ref = m[i0:i1 + 1]
        if r.n != ref.n:
            errs.append(f"{rel} range[{d0},{d1}]: 条数 {r.n} != {ref.n}")
            continue
        if ref.date_str != r.date_str:
            errs.append(f"{rel} range[{d0},{d1}]: 日期不一致 {r.date_str[:2]} vs {ref.date_str[:2]}")
            continue
        for f in ("open", "high", "low", "close", "amount", "volume"):
            if not _eq_arrays(getattr(ref, f), getattr(r, f)):
                errs.append(f"{rel} range[{d0},{d1}]: {f} 不一致")

    # 单边区间
    r1 = read_range(path, ds[-1], None)
    if r1.n != 1:
        errs.append(f"{rel}: read_range(start=末日) 条数 {r1.n} != 1")
    r2 = read_range(path, None, ds[0])
    if r2.n != 1:
        errs.append(f"{rel}: read_range(end=首日) 条数 {r2.n} != 1")
    return errs


def check_hybrid(vipdoc: Path, rel: str) -> list[str]:
    """HybridClient 快路径 vs 旧逻辑 (全量解析 + 切片)。

    显式传 market 而非依赖 market_of 的代码前缀推断:
    000xxx 段沪深二义 (000001 既是上证指数也是平安银行), 靠前缀无法判市场;
    本测试的目标是「快路径 vs 慢路径」的等价性, 不该被市场推断问题干扰。
    """
    from tdxrs.constants import MARKET_BJ, MARKET_SH, MARKET_SZ
    from tdxrs.hybrid import HybridClient, read_local_day

    _MKT = {"sh": MARKET_SH, "sz": MARKET_SZ, "bj": MARKET_BJ}
    path = vipdoc / rel
    m_ref = read_local_day(path)          # 旧逻辑的原料
    total = len(m_ref)
    errs = []

    hc = HybridClient(vipdoc_dir=vipdoc, client=_NoServer())
    stem = rel.split("/")[-1][:-4]        # sh/lday/sh600519.day -> sh600519
    market = _MKT[rel.split("/")[0]]
    code = stem[2:]

    for count in [1, 5, 30, 250, 800]:
        if count > total:
            continue
        r = hc.get_daily_bars(code, count=count, market=market,
                              persist=False, validate=False)
        want = m_ref[-count:]
        if r["source"] != "local":
            errs.append(f"{rel} count={count}: source={r['source']} != local")
        if r["local_count"] != total:
            errs.append(f"{rel} count={count}: local_count={r['local_count']} != {total}")
        if r["bars"] != want:
            errs.append(f"{rel} count={count}: bars 与旧逻辑不一致 "
                        f"(len {len(r['bars'])} vs {len(want)})")

    # 边界: count=0 -> 旧实现 local[-0:] 返回全部
    r0 = hc.get_daily_bars(code, count=0, market=market, persist=False, validate=False)
    if len(r0["bars"]) != total:
        errs.append(f"{rel} count=0: bars={len(r0['bars'])} != {total} (旧行为是全部)")

    # 边界: count > 文件条数 -> 本地不足, 服务器为空 -> 回本地全部
    rbig = hc.get_daily_bars(code, count=total * 3, market=market,
                             persist=False, validate=False)
    if len(rbig["bars"]) != total:
        errs.append(f"{rel} count>total: bars={len(rbig['bars'])} != {total}")
    return errs


def check_synthetic() -> list[str]:
    """合成边界: 空文件 / 非 32 倍数 / 不存在。"""
    from tdxrs.local import read_daily, read_tail

    errs = []
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)

        empty = d / "empty.day"
        empty.write_bytes(b"")
        if read_daily(empty).n != 0:
            errs.append("空文件: read_daily 应为 0 条")
        if read_tail(empty, 10).n != 0:
            errs.append("空文件: read_tail 应为 0 条")

        ragged = d / "ragged.day"
        ragged.write_bytes(b"\x00" * (32 * 3 + 7))
        try:
            read_daily(ragged)
            errs.append("非 32 倍数: read_daily 应抛 ValueError")
        except ValueError:
            pass
        if read_tail(ragged, 2, strict=False).n != 2:
            errs.append("非 32 倍数: read_tail(strict=False) 应取前 2 条")

        try:
            read_tail(ragged, -1, strict=False)
            errs.append("负数 n: read_tail 应抛 ValueError")
        except ValueError:
            pass
        if read_tail(ragged, 999, strict=False).n != 3:
            errs.append("n 超过文件条数: read_tail 应返回全部 3 条")

        missing = d / "nope.day"
        try:
            read_daily(missing)
            errs.append("文件不存在: 应抛 FileNotFoundError")
        except FileNotFoundError:
            pass
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vipdoc", default=str(VIPDOC_DEFAULT))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    vipdoc = Path(args.vipdoc)
    if args.all:
        targets = []
        for mkt in ("sh", "sz", "bj"):
            d = vipdoc / mkt / "lday"
            if d.exists():
                targets += [f"{mkt}/lday/{p.name}" for p in sorted(d.glob("*.day"))]
    else:
        data = json.loads(GOLDEN.read_text(encoding="utf-8"))
        targets = [f["rel"] for f in data["files"] if f["kind"] == "day"]
    if args.limit:
        targets = targets[: args.limit]

    print(f"待测 {len(targets)} 个 .day 文件")

    all_errs: list[str] = list(check_synthetic())
    for i, rel in enumerate(targets, 1):
        all_errs += check_tail(vipdoc, rel)
        all_errs += check_range(vipdoc, rel)
        all_errs += check_hybrid(vipdoc, rel)
        if i % 25 == 0:
            print(f"  {i}/{len(targets)}  累计问题 {len(all_errs)}")

    if all_errs:
        print(f"\n[FAIL] {len(all_errs)} 个问题:")
        for e in all_errs[:30]:
            print("  -", e)
        return 1

    print(f"[PASS] {len(targets)} 文件: read_tail / read_range / HybridClient 快路径 全部等价")
    return 0


if __name__ == "__main__":
    sys.exit(main())
