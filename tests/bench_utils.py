"""基准测试公共工具

提供计时、统计、结果收集、报告生成等基础设施。
各测试脚本 `from tests.bench_utils import *` 即可复用。

导出:
  timed(rounds, warmup)    — 装饰器，自动计时多轮
  BenchmarkResult          — 单次测试结果
  Suite                    — 测试套件 (收集 + 对比)
  fmt_ms / speedup         — 格式化
  markdown_report / console_report / json_report  — 报告生成
"""

import json
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Callable, Optional


# ============================================================
# Data structures
# ============================================================

@dataclass
class BenchmarkResult:
    """单次 Benchmark 结果"""
    library: str          # "tdxrs" | "tdxpy"
    method: str           # "reader_daily" | "network_kline" | ...
    label: str            # human-readable description
    times_ms: list        # raw timing samples
    record_count: int = 0
    error: Optional[str] = None
    skipped: bool = False

    @property
    def mean_ms(self) -> float:
        return statistics.mean(self.times_ms) if self.times_ms else 0.0

    @property
    def min_ms(self) -> float:
        return min(self.times_ms) if self.times_ms else 0.0

    @property
    def max_ms(self) -> float:
        return max(self.times_ms) if self.times_ms else 0.0

    @property
    def std_ms(self) -> float:
        return statistics.stdev(self.times_ms) if len(self.times_ms) > 1 else 0.0

    @property
    def p95_ms(self) -> float:
        if not self.times_ms:
            return 0.0
        s = sorted(self.times_ms)
        idx = min(int(len(s) * 0.95), len(s) - 1)
        return s[idx]


@dataclass
class Suite:
    """测试套件 — 收集多个 BenchmarkResult"""
    results: list = field(default_factory=list)
    config: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def add(self, result: BenchmarkResult):
        self.results.append(result)

    def get_pairs(self, method: str) -> list:
        """获取 tdxrs/tdxpy 同名对比对"""
        rs = [r for r in self.results if r.library == "tdxrs" and r.method == method]
        py = [r for r in self.results if r.library == "tdxpy" and r.method == method]
        return [(r, p) for r in rs for p in py if r.label == p.label]

    def tdxrs_times(self, method_filter: Optional[str] = None) -> list:
        """收集 tdxrs 端指定 method 的所有 timing sample"""
        times = []
        for r in self.results:
            if r.skipped or r.error or not r.times_ms:
                continue
            if method_filter and r.method != method_filter:
                continue
            if r.library == "tdxrs":
                times.extend(r.times_ms)
        return times

    def tdxpy_times(self, method_filter: Optional[str] = None) -> list:
        times = []
        for r in self.results:
            if r.skipped or r.error or not r.times_ms:
                continue
            if method_filter and r.method != method_filter:
                continue
            if r.library == "tdxpy":
                times.extend(r.times_ms)
        return times


# ============================================================
# Timing utilities
# ============================================================

def timed(rounds: int = 10, warmup: int = 2):
    """装饰器 — 自动执行 warmup + rounds 轮并收集耗时(ms)

    被装饰函数应返回 (record_count: int) 或 float 或 int。
    """
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Warmup
            for _ in range(warmup):
                try:
                    func(*args, **kwargs)
                except Exception:
                    pass

            # Timed rounds
            samples = []
            records = 0
            for _ in range(rounds):
                t0 = time.perf_counter()
                try:
                    result = func(*args, **kwargs)
                except Exception:
                    continue
                elapsed = (time.perf_counter() - t0) * 1000  # ms
                samples.append(elapsed)
                # Extract record count from return value
                if isinstance(result, (int, float)):
                    records = max(records, int(result))
                elif isinstance(result, (list, tuple)) and len(result) > 0:
                    records = max(records, len(result) if isinstance(result, (list, tuple)) else 0)

            return samples, records
        return wrapper
    return decorator


def timeit(func: Callable, rounds: int = 10, warmup: int = 2):
    """函数式计时 (非装饰器) — 返回 (samples_ms, record_count)"""
    wrapped = timed(rounds, warmup)(func)
    return wrapped()


# ============================================================
# Formatting
# ============================================================

def fmt_ms(value: float) -> str:
    return f"{value:.2f}" if value < 1.0 else f"{value:.1f}"


def speedup(py_ms: float, rs_ms: float) -> str:
    if rs_ms <= 0:
        return "N/A"
    return f"{py_ms / rs_ms:.1f}×"


# ============================================================
# Report generation
# ============================================================

def console_report(suite: Suite):
    """终端 Markdown 表格输出"""
    lines = []
    lines.append(f"## Benchmark Report — {suite.timestamp}")
    lines.append("")
    for k, v in suite.config.items():
        lines.append(f"- **{k}**: {v}")
    lines.append("")
    lines.append("| Library | Method | Label | Mean(ms) | P95(ms) | Min/Max | Records |")
    lines.append("|---------|--------|-------|----------|---------|---------|---------|")
    for r in suite.results:
        if r.skipped:
            lines.append(f"| {r.library} | {r.method} | {r.label} | SKIPPED | — | — | — |")
        elif r.error:
            lines.append(f"| {r.library} | {r.method} | {r.label} | ERROR: {r.error} | — | — | — |")
        else:
            lines.append(
                f"| {r.library} | {r.method} | {r.label} "
                f"| {fmt_ms(r.mean_ms)} | {fmt_ms(r.p95_ms)} "
                f"| {fmt_ms(r.min_ms)}/{fmt_ms(r.max_ms)} "
                f"| {r.record_count} |"
            )
    lines.append("")
    return "\n".join(lines)


def comparison_table(suite: Suite, method: str) -> str:
    """生成 tdxrs vs tdxpy 对比表"""
    pairs = suite.get_pairs(method)
    if not pairs:
        return f"No comparison pairs for method={method}"

    lines = [
        f"### {method}",
        "",
        "| Label | tdxrs (ms) | tdxpy (ms) | Speedup |",
        "|-------|-----------:|-----------:|:------:|",
    ]
    for rs, py in pairs:
        sp = speedup(py.mean_ms, rs.mean_ms)
        lines.append(f"| {rs.label} | {fmt_ms(rs.mean_ms)} | {fmt_ms(py.mean_ms)} | **{sp}** |")

    # Summary row
    rs_all = suite.tdxrs_times(method)
    py_all = suite.tdxpy_times(method)
    if rs_all and py_all:
        rs_m = statistics.mean(rs_all)
        py_m = statistics.mean(py_all)
        sp = speedup(py_m, rs_m)
        lines.append(f"| **Overall** | {fmt_ms(rs_m)} | {fmt_ms(py_m)} | **{sp}** |")

    return "\n".join(lines)


def markdown_report(suite: Suite, path: Optional[str] = None) -> str:
    """完整的 Markdown 报告

    输出到 path (如提供), 并返回内容字符串。
    """
    lines = [f"# tdxrs Benchmark Report", "", f"Generated: {suite.timestamp}", ""]
    if suite.config:
        lines.append("## Configuration")
        for k, v in suite.config.items():
            lines.append(f"- **{k}**: {v}")
        lines.append("")

    # By method
    methods_seen = set()
    for r in suite.results:
        methods_seen.add(r.method)

    lines.append("## Results")
    for method in sorted(methods_seen):
        lines.append("")
        lines.append(f"### {method}")
        lines.append("")
        lines.append("| Library | Label | Mean(ms) | P95(ms) | Std | Records |")
        lines.append("|---------|-------|----------|---------|-----|---------|")
        for r in suite.results:
            if r.method != method:
                continue
            if r.skipped:
                lines.append(f"| {r.library} | {r.label} | SKIPPED | — | — | — |")
            elif r.error:
                lines.append(f"| {r.library} | {r.label} | ERR | — | — | — |")
            else:
                lines.append(
                    f"| {r.library} | {r.label} "
                    f"| {fmt_ms(r.mean_ms)} | {fmt_ms(r.p95_ms)} "
                    f"| {fmt_ms(r.std_ms)} | {r.record_count} |"
                )

        # Comparison if tdxpy results present
        py_times = suite.tdxpy_times(method)
        rs_times = suite.tdxrs_times(method)
        if py_times and rs_times:
            rs_m = statistics.mean(rs_times)
            py_m = statistics.mean(py_times)
            lines.append("")
            lines.append(f"**Overall tdxrs vs tdxpy**: tdxrs={fmt_ms(rs_m)}ms, tdxpy={fmt_ms(py_m)}ms, speedup={speedup(py_m, rs_m)}")

    lines.append("")

    content = "\n".join(lines)
    if path:
        Path(path).write_text(content, encoding="utf-8")
    return content


def json_report(suite: Suite, path: Optional[str] = None) -> str:
    """JSON 格式报告 (适合 CI 集成)"""
    data = {
        "timestamp": suite.timestamp,
        "config": suite.config,
        "results": [
            {
                "library": r.library,
                "method": r.method,
                "label": r.label,
                "mean_ms": r.mean_ms,
                "p95_ms": r.p95_ms,
                "min_ms": r.min_ms,
                "max_ms": r.max_ms,
                "std_ms": r.std_ms,
                "record_count": r.record_count,
                "error": r.error,
                "skipped": r.skipped,
            }
            for r in suite.results
        ],
    }
    content = json.dumps(data, indent=2, ensure_ascii=False)
    if path:
        Path(path).write_text(content, encoding="utf-8")
    return content


# ============================================================
# Path conventions
# ============================================================

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
FIXTURE_DIR = SCRIPT_DIR / "fixtures"
REPORT_DIR = PROJECT_DIR / "docs"
TDXPY_DIR = PROJECT_DIR.parent / "tdxpy"


def init_rust_import():
    """确保 tdxrs Rust 模块可导入"""
    import sys
    sys.path.insert(0, str(PROJECT_DIR))


def init_tdxpy_import():
    """确保 tdxpy 从父级目录可导入"""
    import sys
    sys.path.insert(0, str(TDXPY_DIR.parent))
