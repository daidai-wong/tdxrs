"""CLI 输出格式化

支持 table / json / csv 三种格式。
table 格式使用简易字符串拼接，无额外依赖。
"""

import csv
import json
import sys


def format_output(data, columns, fmt="table", file=None):
    """统一格式化输出

    Args:
        data: list[dict] 数据行
        columns: list[tuple(key, header, width?)] 列定义
        fmt: "table" / "json" / "csv"
        file: 输出目标，默认 stdout
    """
    out = file or sys.stdout

    if fmt == "json":
        _write_json(data, out)
    elif fmt == "csv":
        _write_csv(data, columns, out)
    else:
        _write_table(data, columns, out)


def _write_json(data, out):
    json.dump(data, out, ensure_ascii=False, indent=2, default=str)
    out.write("\n")


def _write_csv(data, columns, out):
    keys = [c[0] for c in columns]
    headers = [c[1] for c in columns]
    writer = csv.writer(out)
    writer.writerow(headers)
    for row in data:
        writer.writerow(row.get(k, "") for k in keys)


def _write_table(data, columns, out):
    if not data:
        out.write("(无数据)\n")
        return

    # 计算列宽
    keys = [c[0] for c in columns]
    headers = [c[1] for c in columns]
    widths = []

    for i, (key, header, *rest) in enumerate(columns):
        if rest:
            widths.append(rest[0])
            continue
        # 自动宽度: header 宽度 vs 数据最大宽度
        hw = _display_width(header)
        dw = max(_display_width(str(row.get(key, ""))) for row in data) if data else 0
        widths.append(max(hw, dw))

    # 表头
    header_line = "│ " + " │ ".join(
        _pad(h, widths[i]) for i, h in enumerate(headers)
    ) + " │"
    sep_top = "┌─" + "─┬─".join("─" * w for w in widths) + "─┐"
    sep_mid = "├─" + "─┼─".join("─" * w for w in widths) + "─┤"
    sep_bot = "└─" + "─┴─".join("─" * w for w in widths) + "─┘"

    out.write(sep_top + "\n")
    out.write(header_line + "\n")
    out.write(sep_mid + "\n")

    for row in data:
        cells = []
        for i, key in enumerate(keys):
            val = str(row.get(key, ""))
            cells.append(_pad(val, widths[i]))
        out.write("│ " + " │ ".join(cells) + " │\n")

    out.write(sep_bot + "\n")


def _display_width(s):
    """简易显示宽度（中文算2，ASCII算1）"""
    w = 0
    for ch in s:
        if ord(ch) > 0x7F:
            w += 2
        else:
            w += 1
    return w


def _pad(s, width):
    """按显示宽度右填充空格"""
    diff = width - _display_width(s)
    if diff <= 0:
        return s
    return s + " " * diff


def truncate(s, maxlen=30):
    """截断过长字符串"""
    s = str(s)
    if len(s) <= maxlen:
        return s
    return s[: maxlen - 1] + "…"
