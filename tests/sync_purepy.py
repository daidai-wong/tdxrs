# -*- coding: utf-8 -*-
"""把源码树里的**纯 Python 模块**同步到已安装的 tdxrs 包 (开发用)。

为什么需要它
------------
`tdxrs` 是 maturin 构建的扩展包, 纯 Python 模块 (`local.py` / `boundary.py` /
`arrow_cache.py` / `hybrid.py` / ...) 会随 wheel 一起装进 site-packages。
改了源码后若不重装, `import tdxrs` 拿到的仍是**旧副本** —— 于是测试跑在过期代码上,
现象是「明明改了却毫无效果」或「报错行号和源码对不上」。

Rust 侧改动仍然必须走 `maturin develop` / `rebuild_tdxrs.py`; 本脚本只处理
**不需要重新编译**的那部分, 因此比全量重建快得多 (秒级)。

用法:
    python tests/sync_purepy.py            # 同步
    python tests/sync_purepy.py --check    # 只报告差异, 不写
    python tests/sync_purepy.py --list     # 列出会被同步的文件
退出码: 0 = 一致/同步成功; 1 = --check 时存在差异或目标不可写
"""
from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
REPO = SCRIPT_DIR.parent
SRC = REPO / "python" / "tdxrs"
# 只同步纯 Python 模块: 不碰 *.pyd / *.so / *.pyi
PATTERNS = ("*.py",)


def installed_dir() -> Path | None:
    """定位已安装的 tdxrs 包目录 (不含源码树自身)。"""
    try:
        import tdxrs
    except ImportError:
        return None
    d = Path(tdxrs.__file__).parent
    return None if d.resolve() == SRC.resolve() else d


def purepy_files() -> list[Path]:
    out: list[Path] = []
    for pat in PATTERNS:
        out += sorted(SRC.glob(pat))
    return sorted(set(out))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查, 不写")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    dst = installed_dir()
    files = purepy_files()
    if args.list:
        for f in files:
            print(f"{f.relative_to(REPO)}")
        return 0
    if dst is None:
        print("找不到已安装的 tdxrs 包 (或它就在源码树里) -> 无需同步")
        return 0

    diff, same, new = [], [], []
    for f in files:
        t = dst / f.name
        if not t.exists():
            new.append(f.name)
        elif not filecmp.cmp(f, t, shallow=False):
            diff.append(f.name)
        else:
            same.append(f.name)

    if args.check:
        for n in new:
            print(f"  缺失 {n}")
        for n in diff:
            print(f"  不一致 {n}")
        print(f"{dst}: {len(same)} 一致 / {len(diff)} 不一致 / {len(new)} 缺失")
        return 1 if (diff or new) else 0

    copied = []
    for f in files:
        t = dst / f.name
        if not t.exists() or not filecmp.cmp(f, t, shallow=False):
            shutil.copy2(f, t)
            copied.append(f.name)
    print(f"已同步 {len(copied)} 个纯 Python 模块 -> {dst}")
    for n in copied:
        print(f"  {n}")
    if not copied:
        print("  (已是最新)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
