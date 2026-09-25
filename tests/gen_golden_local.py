# -*- coding: utf-8 -*-
"""生成「本地解析黄金语料」—— 后续所有改造阶段的验收基准。

与 gen_fixtures_local.py 的区别:
  gen_fixtures_local.py  -> 供 Rust 回归测试用的**二进制 fixture**(拷贝文件内容)
  本脚本                 -> 供对标测试用的**采样清单 + 完整性指纹**(不拷贝内容)

产出 tests/golden/golden_local.json:
  {
    "generated_at": "...",
    "vipdoc": "D:\\TDX\\vipdoc",
    "files": [
      {"rel": "sh/lday/sh600519.day", "market": "sh", "kind": "day",
       "size": 123456, "n_records": 3858, "sha256": "...",
       "first": {"date": "...", ...}, "last": {"date": "...", ...}},
      ...
    ]
  }

设计要点:
  * 只记录**相对路径**(跨机可移植)与**内容指纹**。vipdoc 每交易日盘后更新,
    因此 sha256 会漂移 -- 断言器检测到漂移时以**实时旧实现输出**为准做比对,
    清单只负责回答"该测哪些文件"和"数据是否变过"。
  * 抽样覆盖: 三个市场 x 长短历史分位 x 证券类型白名单(股票/指数/ETF/北交所)。
  * 同时采样 .lc1 分钟线(32 字节定长, OHLC 为 f32)。

用法:
    python tests/gen_golden_local.py            # 默认 200 日线 + 40 分钟线
    python tests/gen_golden_local.py --n 400
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
GOLDEN_DIR = SCRIPT_DIR / "golden"
OUT = GOLDEN_DIR / "golden_local.json"

DEFAULT_VIPDOC = Path(r"D:\TDX\vipdoc")

# 必须覆盖的样本 (存在才纳入) -- 覆盖各代码段与品种
WHITELIST_DAY = [
    "sh/lday/sh600519.day",   # 沪市主板 贵州茅台
    "sh/lday/sh601398.day",   # 沪市主板 工商银行
    "sh/lday/sh688981.day",   # 科创板 中芯国际
    "sh/lday/sh000001.day",   # 上证指数
    "sh/lday/sh000300.day",   # 沪深300
    "sh/lday/sh510300.day",   # 沪深300ETF
    "sh/lday/sh511990.day",   # 货币ETF
    "sz/lday/sz000001.day",   # 深市主板 平安银行
    "sz/lday/sz000002.day",   # 深市主板 万科A
    "sz/lday/sz300750.day",   # 创业板 宁德时代
    "sz/lday/sz399001.day",   # 深证成指
    "sz/lday/sz159915.day",   # 创业板ETF
    "bj/lday/bj430047.day",   # 北交所
    "bj/lday/bj831010.day",
]

WHITELIST_MIN = [
    "sh/minline/sh600519.lc1",
    "sz/minline/sz000001.lc1",
    "sh/minline/sh000001.lc1",
]

MARKETS = ("sh", "sz", "bj")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def quantile_pick(files: list[Path], k: int) -> list[Path]:
    """按文件大小(≈历史长度)分位采样, 覆盖短/中/长历史。"""
    if not files:
        return []
    files = sorted(files, key=lambda p: p.stat().st_size)
    if len(files) <= k:
        return files
    idx = sorted({int(i * (len(files) - 1) / (k - 1)) for i in range(k)})
    return [files[i] for i in idx]


def sample(vipdoc: Path, kind: str, per_market: int, whitelist: list[str]) -> list[Path]:
    """采样: 白名单优先 + 各市场分位采样, 去重。"""
    picked: dict[str, Path] = {}

    for rel in whitelist:
        p = vipdoc / rel
        if p.exists():
            picked[rel] = p

    sub = "lday" if kind == "day" else "minline"
    ext = ".day" if kind == "day" else ".lc1"

    for mkt in MARKETS:
        d = vipdoc / mkt / sub
        if not d.exists():
            continue
        files = [p for p in d.glob(f"*{ext}")]
        for p in quantile_pick(files, per_market):
            picked[f"{mkt}/{sub}/{p.name}"] = p

    return [picked[k] for k in sorted(picked)]


def describe(path: Path, vipdoc: Path, kind: str) -> dict:
    """记录一个文件: 指纹 + 规模 + 首末记录(经旧实现解析, 作为参考值快照)。"""
    from tdxrs._internal import DailyBarReader, LcMinBarReader

    size = path.stat().st_size
    rec = {
        "rel": str(path.relative_to(vipdoc)).replace("\\", "/"),
        "market": path.parent.parent.name,
        "kind": kind,
        "size": size,
        "n_records": size // 32,
        "sha256": sha256_of(path),
    }

    try:
        if kind == "day":
            tuples = DailyBarReader().parse_file_tuples(str(path))
        else:
            tuples = LcMinBarReader().parse_file_tuples(str(path))
    except Exception as e:                      # 解析失败也要留痕, 供断言器复现
        rec["error"] = f"{type(e).__name__}: {e}"
        return rec

    if len(tuples) != rec["n_records"]:
        rec["warning"] = f"解析条数 {len(tuples)} != size//32 {rec['n_records']}"

    if tuples:
        f, l = tuples[0], tuples[-1]
        if kind == "day":
            rec["first"] = {"date": f[0], "open": f[1], "high": f[2], "low": f[3],
                            "close": f[4], "amount": f[5], "volume": f[6]}
            rec["last"] = {"date": l[0], "open": l[1], "high": l[2], "low": l[3],
                           "close": l[4], "amount": l[5], "volume": l[6]}
        else:
            rec["first"] = {"date": f[0], "open": f[1], "high": f[2], "low": f[3],
                            "close": f[4], "amount": f[5], "volume": f[6],
                            "hour": f[10], "minute": f[11]}
            rec["last"] = {"date": l[0], "open": l[1], "high": l[2], "low": l[3],
                           "close": l[4], "amount": l[5], "volume": l[6],
                           "hour": l[10], "minute": l[11]}
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vipdoc", default=str(DEFAULT_VIPDOC))
    ap.add_argument("--n", type=int, default=60, help="每个市场分位采样条数 (三市场合计 3N)")
    ap.add_argument("--n-min", type=int, default=12, help="每市场分钟线采样条数")
    args = ap.parse_args()

    vipdoc = Path(args.vipdoc)
    if not vipdoc.exists():
        print(f"vipdoc 不存在: {vipdoc}", file=sys.stderr)
        return 2

    day_files = sample(vipdoc, "day", args.n, WHITELIST_DAY)
    min_files = sample(vipdoc, "min", args.n_min, WHITELIST_MIN)

    print(f"采样: {len(day_files)} 个 .day + {len(min_files)} 个 .lc1")

    files = []
    for p in day_files:
        files.append(describe(p, vipdoc, "day"))
    for p in min_files:
        files.append(describe(p, vipdoc, "min"))

    total_records = sum(f["n_records"] for f in files)
    total_bytes = sum(f["size"] for f in files)
    errors = [f["rel"] for f in files if "error" in f]

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "vipdoc": str(vipdoc),
        "n_files": len(files),
        "total_records": total_records,
        "total_bytes": total_bytes,
        "errors": errors,
        "files": files,
    }

    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"总记录数 {total_records:,}  总字节 {total_bytes/1048576:.2f} MB")
    if errors:
        print(f"解析失败 {len(errors)} 个: {errors[:5]}")
    print(f"已写入 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
