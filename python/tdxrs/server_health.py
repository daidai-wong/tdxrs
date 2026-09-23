"""tdxrs 服务器健康筛查模块

对通达信行情服务器做多级健康检查、综合评分与排序，优选质量最好的服务器。

检查层级 (四级体检):
  1. TCP 连接      —— Rust 层 probe_servers
  2. 协议握手      —— 同上
  3. 元数据 API    —— get_security_count 等价报文 (证券数量)
  4. 行情数据面    —— 真实拉取 600519/000001 实时行情 (+日K 抽查)
                     用于区分 "活着但返空" 的服务器 (2026-07 服务端协议更新后
                     行情报文族大面积返空, 2026-09-24 实测仅国泰君安集群
                     117.34.114.x 幸存; 仅靠前三层会误判为健康)

评分 (0-100, 越高越好):
    score = 100 - min(加权延时ms / 10, 60) + 25*行情可用 + 10*日K可用
    加权延时 = 0.4*tcp + 0.3*握手 + 0.3*API (毫秒)
    行情可用时: 加权延时 = 0.7*上述 + 0.3*行情请求延时

级别:
    A >= 85 优秀 | B >= 70 良好 | C >= 55 可用 | D > 0 勉强 | F 失联

用法:
    from tdxrs import screen_servers, best_server

    report = screen_servers()            # 全量体检, 按评分降序返回
    ip, port = best_server()             # 直接取最优服务器 (带 24h 缓存)

    CLI:
        python -m tdxrs servers              # 全量筛查表格
        python -m tdxrs servers --top 10     # 只看前 10
        python -m tdxrs servers --json       # 完整 JSON 报告
        python -m tdxrs servers --no-data    # 跳过数据面探测 (更快)
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from tdxrs._internal import TdxHqClient

# 筛查结果缓存位置
DEFAULT_CACHE_PATH = Path.home() / ".tdxrs" / "servers.json"

# 数据面探测用的股票 (流动性最好、永远有效的两只: 贵州茅台 / 平安银行)
PROBE_STOCKS = ((1, "600519"), (0, "000001"))

# 级别阈值: (最低分, 级别, 说明)
_GRADES = (
    (85, "A", "优秀"),
    (70, "B", "良好"),
    (55, "C", "可用"),
    (0, "D", "勉强"),
)


def grade_of(score: float) -> tuple[str, str]:
    """分数 -> (级别, 说明)。失联服务器由 screen_servers 直接标 F。"""
    for threshold, grade, label in _GRADES:
        if score >= threshold:
            return grade, label
    return "D", "勉强"


def compute_score(
    tcp_ms: float,
    hs_ms: float,
    api_ms: float,
    data_ms: float | None = None,
    data_ok: bool = False,
    bars_ok: bool = False,
) -> float:
    """综合评分 (0-100)。

    传输层加权延时为基础, 行情数据可用性是核心加分项——
    当前服务端异常背景下, "有数据" 比 "响应快" 更稀缺。
    """
    lat = 0.4 * tcp_ms + 0.3 * hs_ms + 0.3 * api_ms
    if data_ok and data_ms is not None:
        lat = 0.7 * lat + 0.3 * data_ms
    score = 100.0 - min(lat / 10.0, 60.0)
    if data_ok:
        score += 25.0
    if bars_ok:
        score += 10.0
    return round(max(0.0, min(100.0, score)), 1)


def _known_server_universe() -> list[tuple[str, str, int]]:
    """已知服务器全集 (名称, ip, port), 用于标记探测失败的失联服务器。"""
    try:
        from tdxrs.downloader import _DEFAULT_SERVERS

        return [(name, ip, port) for name, ip, port in _DEFAULT_SERVERS]
    except Exception:
        return []


def _probe_data_plane_one(ip: str, port: int, timeout: float) -> dict:
    """单台服务器数据面探测: 实时行情 + 日K 抽查。

    返回: {data_ms, data_ok, quotes_n, bars_ok, error}
    """
    out = {"data_ms": None, "data_ok": False, "quotes_n": 0,
           "bars_ok": False, "bars_n": 0, "error": None}
    client = TdxHqClient()
    try:
        if not client.connect(ip, port, timeout):
            out["error"] = "connect failed"
            return out
    except Exception as e:
        out["error"] = f"connect: {e}"
        return out

    try:
        t0 = time.perf_counter()
        quotes = client.get_security_quotes(list(PROBE_STOCKS))
        out["data_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        out["quotes_n"] = len(quotes) if quotes else 0
        # 价格必须 > 0 才算真数据 (防全零载荷误判)
        out["data_ok"] = bool(quotes) and any(
            (q.get("price") or 0) > 0 for q in quotes
        )
    except Exception as e:
        out["error"] = f"quotes: {e}"
        client.disconnect()
        return out

    if out["data_ok"]:
        try:
            bars = client.get_security_bars(4, 1, "600519", 0, 10)  # 日K 抽查
            out["bars_n"] = len(bars) if bars else 0
            out["bars_ok"] = out["bars_n"] > 0
        except Exception as e:
            out["error"] = f"bars: {e}"

    try:
        client.disconnect()
    except Exception:
        pass
    return out


def _probe_data_plane_parallel(
    pairs: list[tuple[str, int]], timeout: float, workers: int
) -> dict[tuple[str, int], dict]:
    """并发数据面探测 (依赖 GIL 释放, 多线程真并行)。"""
    results: dict[tuple[str, int], dict] = {}
    workers = max(1, min(workers, len(pairs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(_probe_data_plane_one, ip, port, timeout): (ip, port)
            for ip, port in pairs
        }
        for fut in as_completed(futs):
            ip, port = futs[fut]
            try:
                results[(ip, port)] = fut.result()
            except Exception as e:  # 理论上不发生, 兜底
                results[(ip, port)] = {
                    "data_ms": None, "data_ok": False, "quotes_n": 0,
                    "bars_ok": False, "bars_n": 0, "error": str(e),
                }
    return results


def _sort_results(results: list[dict]) -> list[dict]:
    """评分降序, 同分按 API 延时升序。"""
    def key(r):
        api = r.get("api_ms")
        return (-r["score"], api if api is not None else float("inf"))

    return sorted(results, key=key)


def _mark_missing(results: list[dict], universe: list[tuple[str, str, int]]) -> list[dict]:
    """把全集中有、但探测结果里没有的服务器标记为失联 (F)。"""
    seen = {(r["ip"], r["port"]) for r in results}
    for name, ip, port in universe:
        if (ip, port) in seen:
            continue
        results.append({
            "name": name, "ip": ip, "port": port,
            "tcp_ms": None, "hs_ms": None, "api_ms": None,
            "data_ms": None, "data_ok": None, "quotes_n": None,
            "bars_ok": None, "bars_n": None,
            "score": 0.0, "grade": "F", "grade_label": "失联",
            "status": "失联 (探测失败)", "error": "probe failed",
        })
    return results


def screen_servers(
    timeout: float = 3.0,
    data_probe: bool = True,
    workers: int = 8,
    save: bool = True,
    cache_path: str | Path | None = None,
) -> list[dict]:
    """全量服务器健康筛查。

    Args:
        timeout: 单步超时秒数 (TCP/握手/API/数据请求共用)
        data_probe: 是否做行情数据面探测 (第 4 层)。False 时只测传输层, 更快
        workers: 数据面并发线程数
        save: 是否写缓存 (best_server 复用)
        cache_path: 缓存路径, 默认 ~/.tdxrs/servers.json

    Returns:
        list[dict], 按评分降序。每项含:
        name/ip/port, tcp_ms/hs_ms/api_ms, data_ms/data_ok/quotes_n/bars_ok,
        score/grade/grade_label/status
    """
    # 第 1-3 层: Rust 传输层探测 (已按 API 延时升序)
    prober = TdxHqClient()
    raw = prober.probe_servers(timeout)

    # 第 4 层: 数据面并发探测
    data_map: dict[tuple[str, int], dict] = {}
    if data_probe and raw:
        pairs = [(ip, port) for _, ip, port, *_ in raw]
        data_map = _probe_data_plane_parallel(pairs, timeout, workers)

    results: list[dict] = []
    for name, ip, port, tcp_ms, hs_ms, api_ms in raw:
        d = data_map.get((ip, port), {})
        data_ok = d.get("data_ok", False)
        score = compute_score(
            tcp_ms, hs_ms, api_ms,
            data_ms=d.get("data_ms"),
            data_ok=data_ok,
            bars_ok=d.get("bars_ok", False),
        )
        grade, label = grade_of(score)
        if data_probe:
            status = "数据OK" if data_ok else "行情返空"
            if d.get("error"):
                status += f" ({d['error']})"
        else:
            status = "未探测数据面"
        results.append({
            "name": name, "ip": ip, "port": port,
            "tcp_ms": round(tcp_ms, 1), "hs_ms": round(hs_ms, 1),
            "api_ms": round(api_ms, 1),
            "data_ms": d.get("data_ms"), "data_ok": data_ok if data_probe else None,
            "quotes_n": d.get("quotes_n") if data_probe else None,
            "bars_ok": d.get("bars_ok") if data_probe else None,
            "bars_n": d.get("bars_n") if data_probe else None,
            "score": score, "grade": grade, "grade_label": label,
            "status": status, "error": d.get("error"),
        })

    results = _mark_missing(results, _known_server_universe())
    results = _sort_results(results)

    if save:
        try:
            save_report(results, cache_path or DEFAULT_CACHE_PATH)
        except Exception:
            pass  # 缓存失败不影响筛查结果
    return results


def save_report(results: list[dict], path: str | Path) -> Path:
    """筛查报告落盘 (JSON, 含时间戳)。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "count": len(results),
        "results": results,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def load_report(path: str | Path) -> dict | None:
    """读取缓存报告, 不存在返回 None。"""
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def best_server(
    cache_path: str | Path | None = None,
    max_age_hours: float = 24.0,
    refresh: bool = False,
    **screen_kwargs,
) -> tuple[str, int] | None:
    """取最优服务器 (ip, port)。

    默认优先读 24h 内的缓存; 无缓存/过期/refresh=True 时重新筛查。
    返回 None 表示全部失联。
    """
    path = Path(cache_path or DEFAULT_CACHE_PATH)
    report = load_report(path)
    fresh = False
    if report and not refresh:
        ts = report.get("timestamp", "")
        try:
            age_h = (
                datetime.now() - datetime.fromisoformat(ts)
            ).total_seconds() / 3600.0
            fresh = 0 <= age_h <= max_age_hours
        except Exception:
            fresh = False

    if not fresh:
        results = screen_servers(save=True, cache_path=path, **screen_kwargs)
    else:
        results = report.get("results", [])

    for r in results:
        if r.get("grade") != "F" and r.get("ip"):
            return r["ip"], r["port"]
    return None


def print_report(results: list[dict], top: int = 0, file=sys.stdout) -> None:
    """打印筛查报告表格。"""
    from tdxrs.cli_format import format_output

    def fmt_ms(v):
        return f"{v:.0f}" if isinstance(v, (int, float)) else "—"

    columns = [
        ("服务器", "服务器", 12),
        ("地址", "地址", 24),
        ("TCP", "TCP", 7),
        ("握手", "握手", 7),
        ("API", "API", 7),
        ("行情", "行情", 7),
        ("数据面", "数据面", 10),
        ("评分", "评分", 7),
        ("级别", "级别", 8),
        ("状态", "状态", 14),
    ]
    rows = []
    for r in results[:top] if top else results:
        rows.append({
            "服务器": r["name"],
            "地址": f"{r['ip']}:{r['port']}",
            "TCP": fmt_ms(r["tcp_ms"]),
            "握手": fmt_ms(r["hs_ms"]),
            "API": fmt_ms(r["api_ms"]),
            "行情": fmt_ms(r["data_ms"]),
            "数据面": ("OK" if r["data_ok"] else
                     "返空" if r["data_ok"] is False else "—"),
            "评分": f"{r['score']:.1f}",
            "级别": f"{r['grade']} ({r['grade_label']})" if r["grade"] != "F" else "F",
            "状态": r["status"],
        })
    format_output(rows, columns, "table", file=file)


if __name__ == "__main__":
    rep = screen_servers()
    alive = [r for r in rep if r["grade"] != "F"]
    data_ok_n = sum(1 for r in alive if r.get("data_ok"))
    print(f"可达: {len(alive)}/{len(rep)}   行情数据可用: {data_ok_n}/{len(alive)}\n")
    print_report(rep)
