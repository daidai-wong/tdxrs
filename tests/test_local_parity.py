# -*- coding: utf-8 -*-
"""本地解析一致性断言器 -- 所有改造阶段的验收闸门。

作用: 判定「候选实现」的输出是否与「参考实现」逐行逐字段一致。

参考实现 = 现有 PyO3 路径 (DailyBarReader.parse_file_tuples / LcMinBarReader.parse_file_tuples)
候选实现 = 待验收的新路径 (目前: baseline 自比; 后续: tdxrs.local 的零拷贝读取器)

为什么用「旧实现实时输出」而非 golden 里冻结的期望值:
  vipdoc 每交易日盘后更新, 冻结值必然过期。golden 清单只负责回答
  「该测哪些文件」(采样清单) 与「数据是否变过」(sha256 漂移检测);
  一致性判据始终是「同一份字节, 两条路径给出的结果是否相同」。

用法:
    python tests/test_local_parity.py                        # 默认清单 + 全部已注册实现
    python tests/test_local_parity.py --impl baseline        # 只跑基线自比(环境自检)
    python tests/test_local_parity.py --all                  # 全量 9233 文件(慢, ~3min)
    python tests/test_local_parity.py --limit 20             # 快速抽查
    python tests/test_local_parity.py --json out.json

退出码: 0 = 全部一致; 1 = 存在不一致; 2 = 环境/语料错误
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).parent
GOLDEN = SCRIPT_DIR / "golden" / "golden_local.json"
VIPDOC_DEFAULT = Path(r"D:\TDX\vipdoc")

PRICE_TOL = 1e-9      # 价格: f64 精确比较(允许最后一位浮点误差)
AMOUNT_TOL = 1e-3     # 成交额: .day 里是 f32, 允许相对误差
VOLUME_TOL = 0.5      # 成交量: 整数, 允许 0.5 以内(防浮点/整数混用)

DAY_FIELDS = ("open", "high", "low", "close", "amount", "volume")
MIN_EXTRA = ("hour", "minute")


# ============================================================
# 统一视图: 把任意实现的输出规约成可比较的 numpy 形态
# ============================================================

def _date_str_day(raw) -> str:
    """u32 YYYYMMDD 或字符串 -> 'YYYY-MM-DD'。"""
    if isinstance(raw, str):
        return raw
    return f"{raw // 10000:04d}-{raw // 100 % 100:02d}-{raw % 100:02d}"


def view_from_tuples(tuples, kind: str) -> dict:
    """参考实现输出 (list of tuple) -> 统一视图。"""
    n = len(tuples)
    out = {"n": n, "dates": [], "kind": kind}
    if n == 0:
        for f in DAY_FIELDS:
            out[f] = np.empty(0, dtype=np.float64)
        return out

    out["dates"] = [t[0] for t in tuples]
    for i, f in enumerate(DAY_FIELDS, start=1):
        out[f] = np.fromiter((t[i] for t in tuples), dtype=np.float64, count=n)
    if kind == "min":
        out["hour"] = np.fromiter((t[10] for t in tuples), dtype=np.int64, count=n)
        out["minute"] = np.fromiter((t[11] for t in tuples), dtype=np.int64, count=n)
    return out


def view_from_matrix(m, kind: str) -> dict:
    """候选实现输出 (tdxrs.local.Matrix) -> 统一视图。"""
    n = int(m.n)
    out = {"n": n, "kind": kind}
    if kind == "day":
        out["dates"] = m.date_str
        for f in DAY_FIELDS:
            out[f] = np.asarray(getattr(m, f), dtype=np.float64)
    else:
        # 参考实现的分钟线 date 形如 "YYYY-MM-DD HH:MM"
        base = m.date_str
        hh = np.asarray(m.hour)
        mm = np.asarray(m.minute)
        out["dates"] = [f"{base[i]} {hh[i]:02d}:{mm[i]:02d}" for i in range(n)]
        for f in DAY_FIELDS:
            out[f] = np.asarray(getattr(m, f), dtype=np.float64)
        out["hour"] = hh.astype(np.int64)
        out["minute"] = mm.astype(np.int64)
    return out


# ============================================================
# 比对
# ============================================================

def compare(ref: dict, got: dict, rel: str, kind: str, max_report: int = 5) -> list[dict]:
    diffs: list[dict] = []

    if ref["n"] != got["n"]:
        diffs.append({"rel": rel, "field": "__n_records__",
                      "detail": f"条数 {ref['n']} != {got['n']}"})
        return diffs

    if ref["n"] == 0:
        return diffs

    if ref["dates"] != got["dates"]:
        bad = next((i for i in range(ref["n"]) if ref["dates"][i] != got["dates"][i]), None)
        diffs.append({"rel": rel, "field": "date",
                      "detail": f"首个不一致 index={bad}: {ref['dates'][bad]} != {got['dates'][bad]}"})

    tol_of = {"amount": AMOUNT_TOL, "volume": VOLUME_TOL}
    for f in DAY_FIELDS:
        a, b = ref[f], got[f]
        if f == "amount":
            denom = np.maximum(np.abs(a), 1.0)
            bad_idx = np.flatnonzero(np.abs(a - b) / denom > AMOUNT_TOL)
        else:
            tol = tol_of.get(f, PRICE_TOL)
            bad_idx = np.flatnonzero(np.abs(a - b) > tol)
        if bad_idx.size:
            i = int(bad_idx[0])
            sample = [{"i": int(j), "ref": float(a[j]), "got": float(b[j])}
                      for j in bad_idx[:max_report]]
            diffs.append({"rel": rel, "field": f,
                          "detail": f"{bad_idx.size} 处不一致 (共 {ref['n']} 条)",
                          "samples": sample})

    if kind == "min":
        for f in MIN_EXTRA:
            if not np.array_equal(ref[f], got[f]):
                bad = np.flatnonzero(ref[f] != got[f])
                diffs.append({"rel": rel, "field": f,
                              "detail": f"{bad.size} 处不一致, 首个 index={int(bad[0])}"})
    return diffs


# ============================================================
# 实现注册表
# ============================================================

def impl_baseline(vipdoc: Path, rel: str, kind: str):
    """参考实现本身 (自比, 用于环境自检)。"""
    from tdxrs._internal import DailyBarReader, LcMinBarReader

    p = str(vipdoc / rel)
    if kind == "day":
        return DailyBarReader().parse_file_tuples(p)
    return LcMinBarReader().parse_file_tuples(p)


def _register(impls: dict) -> None:
    impls["baseline"] = {
        "fn": impl_baseline,
        "kind": "tuples",
        "desc": "现有 PyO3 路径 (parse_file_tuples) -- 参考实现自比",
    }

    # 候选: 零拷贝矩阵读取器 (P2)
    try:
        from tdxrs import local as _local           # noqa: F401

        def impl_local(vipdoc: Path, rel: str, kind: str):
            p = vipdoc / rel
            if kind == "day":
                return _local.read_daily(p)
            return _local.read_lc(p)

        impls["local"] = {
            "fn": impl_local,
            "kind": "matrix",
            "desc": "tdxrs.local 零拷贝 numpy 矩阵读取器",
        }
    except Exception:
        pass


# ============================================================
# 主流程
# ============================================================

def collect_targets(vipdoc: Path, golden_path: Path, all_files: bool) -> list[tuple[str, str]]:
    """-> [(rel, kind), ...]"""
    if all_files:
        targets = []
        for mkt in ("sh", "sz", "bj"):
            for sub, ext, kind in (("lday", ".day", "day"), ("minline", ".lc1", "min")):
                d = vipdoc / mkt / sub
                if d.exists():
                    targets += [(f"{mkt}/{sub}/{p.name}", kind) for p in sorted(d.glob(f"*{ext}"))]
        return targets

    if not golden_path.exists():
        raise SystemExit(f"黄金语料不存在: {golden_path}\n先运行: python tests/gen_golden_local.py")
    data = json.loads(golden_path.read_text(encoding="utf-8"))
    return [(f["rel"], f["kind"]) for f in data["files"]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vipdoc", default=str(VIPDOC_DEFAULT))
    ap.add_argument("--golden", default=str(GOLDEN))
    ap.add_argument("--impl", default=None, help="只跑指定实现 (默认全部已注册)")
    ap.add_argument("--all", action="store_true", help="全量 vipdoc (9233 文件)")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个文件")
    ap.add_argument("--json", default=None, help="结果落盘 JSON")
    args = ap.parse_args()

    vipdoc = Path(args.vipdoc)
    targets = collect_targets(vipdoc, Path(args.golden), args.all)
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("没有待测文件", file=sys.stderr)
        return 2

    impls: dict = {}
    _register(impls)
    if args.impl:
        if args.impl not in impls:
            print(f"未注册的实现: {args.impl} (已注册: {list(impls)})", file=sys.stderr)
            return 2
        impls = {args.impl: impls[args.impl]}

    print(f"待测文件 {len(targets)}  实现 {list(impls)}")
    report = {"targets": len(targets), "impls": {}, "t0": None}

    overall_ok = True
    for name, spec in impls.items():
        all_diffs: list[dict] = []
        n_records = 0
        t0 = time.perf_counter()

        for i, (rel, kind) in enumerate(targets, 1):
            path = vipdoc / rel
            if not path.exists():
                all_diffs.append({"rel": rel, "field": "__missing__", "detail": "文件不存在"})
                continue
            try:
                ref = view_from_tuples(impl_baseline(vipdoc, rel, kind), kind)
            except Exception as e:
                all_diffs.append({"rel": rel, "field": "__ref_error__",
                                  "detail": f"{type(e).__name__}: {e}"})
                continue

            try:
                raw = spec["fn"](vipdoc, rel, kind)
            except Exception as e:
                all_diffs.append({"rel": rel, "field": "__impl_error__",
                                  "detail": f"{type(e).__name__}: {e}"})
                continue

            if spec["kind"] == "tuples":
                got = view_from_tuples(raw, kind)
            else:
                got = view_from_matrix(raw, kind)

            n_records += ref["n"]
            all_diffs += compare(ref, got, rel, kind)

            if i % 50 == 0:
                print(f"  [{name}] {i}/{len(targets)} ...")

        wall = time.perf_counter() - t0
        ok = not all_diffs
        overall_ok = overall_ok and ok
        report["impls"][name] = {
            "desc": spec["desc"],
            "ok": ok,
            "files": len(targets),
            "records": n_records,
            "wall_s": round(wall, 3),
            "diff_count": len(all_diffs),
            "diffs": all_diffs[:40],
        }
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}: {len(targets)} 文件 / {n_records:,} 条 / "
              f"{wall:.2f}s / {len(all_diffs)} 处不一致")

    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入 {args.json}")

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
