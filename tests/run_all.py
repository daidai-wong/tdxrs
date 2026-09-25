# -*- coding: utf-8 -*-
"""统一跑全部 Python 测试 runner (计划 §P5「CI 门禁」的正确性半边)。

本仓库的测试是**独立 runner**(pytest 插件在本机装不齐, 见项目备忘), 因此这里
按类别调度:

  LOCAL   : 只依赖本机代码与隔离 venv, 任何环境都能跑
  VIPDOC  : 需要真实 vipdoc (D:\\TDX\\vipdoc) 或黄金语料 -> 缺失时自动 SKIP
  NETWORK : 需要行情服务器 (117.34.114.x) -> --ci 模式下跳过

用法
----
    python tests/run_all.py                 # 全跑 (含网络)
    python tests/run_all.py --ci            # CI 模式: 跳过 NETWORK, 缺 vipdoc 则 SKIP
    python tests/run_all.py --rust          # 附带跑 cargo test (需 PYO3_PYTHON)
    python tests/run_all.py --only LOCAL    # 只跑某类
退出码: 0 = 全过(含 SKIP); 1 = 有失败
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
REPO = SCRIPT_DIR.parent
VIPDOC_DEFAULT = Path(r"D:\TDX\vipdoc")
GOLDEN = SCRIPT_DIR / "golden" / "golden_local.json"

# (文件, 类别, 额外参数)
RUNNERS = [
    ("test_dataframe_dtype.py", "LOCAL", []),
    ("test_cli_auto_market.py", "LOCAL", []),
    ("test_hybrid.py", "LOCAL", []),
    ("test_local_parity.py", "VIPDOC", []),
    ("test_tail_range.py", "VIPDOC", []),
    ("test_scan_panel.py", "VIPDOC", []),
    ("test_boundary.py", "VIPDOC", []),
    ("test_etf_module.py", "NETWORK", []),
    ("test_f10_module.py", "NETWORK", []),
    ("test_server_health.py", "NETWORK", []),
]


def run_one(path: Path, extra: list[str], env: dict) -> tuple[int, float, str]:
    t0 = time.perf_counter()
    p = subprocess.run([sys.executable, str(path)] + extra,
                       capture_output=True, text=True, cwd=str(REPO), env=env)
    dt = time.perf_counter() - t0
    tail = (p.stdout.strip().splitlines() or [""])[-1]
    return p.returncode, dt, tail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vipdoc", default=str(VIPDOC_DEFAULT))
    ap.add_argument("--ci", action="store_true", help="CI 模式: 跳过 NETWORK")
    ap.add_argument("--rust", action="store_true", help="附带跑 cargo test")
    ap.add_argument("--only", default=None, choices=["LOCAL", "VIPDOC", "NETWORK"])
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    vipdoc = Path(args.vipdoc)
    have_vipdoc = vipdoc.exists() and any((vipdoc / m / "lday").exists()
                                          for m in ("sh", "sz", "bj"))
    have_golden = GOLDEN.exists()
    force_cli = bool(os.environ.get("TDXRS_TEST_FORCE_CLI"))

    if args.list:
        for f, cat, extra in RUNNERS:
            print(f"{cat:8} {f} {' '.join(extra)}")
        return 0

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")

    print("=" * 74)
    print(f"Python 测试 runner  ({'CI 模式' if args.ci else '本地模式'})")
    print(f"vipdoc: {vipdoc} -> {'可用' if have_vipdoc else '不可用'}; "
          f"黄金语料: {'有' if have_golden else '无'}")
    print("=" * 74)

    results: list[tuple[str, str, float, str]] = []
    failed = 0
    for fname, cat, extra in RUNNERS:
        if args.only and cat != args.only:
            continue
        path = SCRIPT_DIR / fname
        if not path.exists():
            results.append((fname, "MISSING", 0.0, ""))
            continue
        if cat == "VIPDOC" and not (have_vipdoc and have_golden):
            results.append((fname, "SKIP", 0.0, "缺 vipdoc/黄金语料"))
            continue
        if cat == "NETWORK" and args.ci:
            results.append((fname, "SKIP", 0.0, "CI 模式跳过网络用例"))
            continue
        cmd_extra = list(extra)
        if cat == "VIPDOC":
            cmd_extra += ["--vipdoc", str(vipdoc)]
        rc, dt, tail = run_one(path, cmd_extra, env)
        status = "PASS" if rc == 0 else ("SKIP" if rc == 2 else "FAIL")
        if status == "FAIL":
            failed += 1
        results.append((fname, status, dt, tail))

    for fname, status, dt, tail in results:
        mark = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP",
                "MISSING": "MISS"}[status]
        print(f"  [{mark}] {fname:28} {dt:6.2f}s  {tail[:60]}")

    if args.rust:
        print("-" * 74)
        t0 = time.perf_counter()
        renv = dict(env)
        # pyo3 编译测试目标需要指向一个真实解释器
        renv.setdefault("PYO3_PYTHON", sys.executable)
        renv.setdefault("VIRTUAL_ENV", str(Path(sys.executable).parent.parent))
        p = subprocess.run(["cargo", "test", "--lib", "--tests"], cwd=str(REPO),
                           capture_output=True, text=True, env=renv)
        dt = time.perf_counter() - t0
        out = p.stdout + p.stderr
        summary = [l for l in out.splitlines() if l.startswith("test result:")
                   or "test result:" in l]
        passed = sum(int(l.split()[3]) for l in summary if len(l.split()) > 3
                     and l.split()[3].isdigit())
        print(f"  [{'PASS' if p.returncode == 0 else 'FAIL'}] "
              f"cargo test --lib --tests   {dt:6.2f}s  通过 {passed} 条")
        if p.returncode != 0:
            failed += 1
            for l in out.splitlines()[-25:]:
                print(f"      {l}")

    n_skip = sum(1 for r in results if r[1] == "SKIP")
    print("-" * 74)
    print(f"合计: {len(results) - failed - n_skip} 通过 / {failed} 失败 / {n_skip} 跳过")
    print("ALL_OK" if failed == 0 else "ALL_FAIL")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
