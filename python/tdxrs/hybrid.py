"""tdxrs 混合数据层: 本地 vipdoc 优先 + 服务器补缺 + 双源数据验证

数据获取策略 (本地优先):
  1. 本地 vipdoc (.day) 足够 (条数 >= 请求数) -> 纯本地返回, 不碰网络
  2. 本地缺失或不全 -> 调服务器拉取, 与本地按日期合并去重
     (重叠日期服务器侧胜出——服务器是权威数据源), 结果可回写本地
  3. 服务器不可用/返空 (如 2026-07 起的行情报文族异常) -> 尽力返回本地数据

双源数据验证:
  本地与服务器都有数据时, 对重叠日期做 OHLC 一致性比对
  (容差 0.011 元, .day 文件价格按 *100 整数存储)。

vipdoc 目录解析优先级:
  显式参数 > 环境变量 TDXRS_VIPDOC > 常见通达信安装路径自动探测

用法:
    from tdxrs.hybrid import HybridClient, get_daily_bars

    hc = HybridClient()                       # 自动探测 vipdoc
    r = hc.get_daily_bars("600519", count=100)
    r["source"]      # "local" / "server" / "local+server" / "server(fq)"
    r["bars"]        # [{"date","open","high","low","close","volume","amount"}, ...]
    r["validation"]  # 双源验证报告 (无重叠时为 None/说明)

    report = hc.validate("600519")            # 独立验证报告

    CLI:
        python -m tdxrs hbars 600519 --count 30
        python -m tdxrs validate 600519

注意:
- 本地 vipdoc 是未复权数据, fq != 0 时直接走服务器 (source="server(fq)")
- volume 口径: 本地 .day 为股数, 服务器 vol 为手数, 合并时保留各自原始口径;
  验证只比对 OHLC 价格, 不比对成交量
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

from tdxrs._internal import DailyBarReader, TdxHqClient
from tdxrs.constants import MARKET_BJ, MARKET_SH, MARKET_SZ
from tdxrs.local import RECORD_SIZE, read_tail

# 常见通达信客户端 vipdoc 路径 (自动探测用)
_VIPDOC_CANDIDATES = (
    r"C:\new_tdx\vipdoc",
    r"D:\new_tdx\vipdoc",
    r"E:\new_tdx\vipdoc",
    r"C:\zd_zsone\vipdoc",
    r"D:\zd_zsone\vipdoc",
    r"C:\ht_zqone\vipdoc",
    r"C:\tdx\vipdoc",
    r"D:\tdx\vipdoc",
)

_MARKET_DIR = {MARKET_SH: "sh", MARKET_SZ: "sz", MARKET_BJ: "bj"}

# .day 价格整数存储 (×100), 比对容差略大于 0.01
_PRICE_TOL = 0.011

_OHLC = ("open", "high", "low", "close")


# ============================================================
# vipdoc 定位与路径
# ============================================================

def locate_vipdoc(extra_candidates=None) -> Path | None:
    """定位本机 vipdoc 目录: 环境变量 > 候选列表 > 常见安装路径。"""
    env = os.environ.get("TDXRS_VIPDOC")
    if env and Path(env).exists():
        return Path(env)
    for p in list(extra_candidates or []) + list(_VIPDOC_CANDIDATES):
        if p and Path(p).exists():
            return Path(p)
    return None


def market_of(code: str) -> int:
    """股票代码 -> 市场常量 (与 cli.auto_market 同规则)。

    注意 000xxx 段沪深二义 (000001 既是上证指数也是平安银行),
    这类代码必须显式传 market 参数。

    北交所代码段按本机 vipdoc 实测补齐: bj/lday 下只有 81/89/92 三种前缀
    (sh 与 sz 目录均无 81/89 前缀, 故无冲突); 43/83/87 为北交所既有段。
    """
    code = str(code)
    if code.startswith(("43", "83", "87", "920", "81", "89")):
        return MARKET_BJ
    if code.startswith(("6", "5", "9")):
        return MARKET_SH
    return MARKET_SZ


def local_day_path(vipdoc_dir, code: str, market: int | None = None) -> Path:
    """vipdoc 目录 + 代码 -> lday 日线文件路径。"""
    market = market if market is not None else market_of(code)
    d = _MARKET_DIR[market]
    return Path(vipdoc_dir) / d / "lday" / f"{d}{code}.day"


# ============================================================
# .day 读写 (与 Downloader._write_tdx / DailyBarReader 完全兼容)
# ============================================================

def _normalize_local(r: dict) -> dict:
    return {
        "date": r["date"],
        "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
        "volume": r["volume"], "amount": r["amount"],
    }


def _normalize_server(b: dict) -> dict:
    dt = str(b.get("datetime", ""))
    return {
        "date": dt[:10] if len(dt) >= 10 else dt,
        "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"],
        "volume": b.get("vol", 0), "amount": b.get("amount", 0),
    }


def read_local_day(path) -> list[dict]:
    """读取本地 .day 文件 -> 标准化记录列表 (升序)。"""
    records = DailyBarReader().parse_file(str(path))
    return [_normalize_local(r) for r in records]


def write_local_day(path, records: list[dict]) -> Path:
    """写 TDX .day 二进制 (可被 DailyBarReader / Downloader 直接读取)。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        for r in records:
            y, m, d = (int(x) for x in str(r["date"]).split("-"))
            # TDX 日期编码: 2004 年起 (year-2004)*2048 + month*100 + day
            date_int = (y - 2004) * 2048 + m * 100 + d if y >= 2004 else y * 10000 + m * 100 + d
            f.write(struct.pack(
                "<IIIIIfII",
                date_int,
                int(round(r["open"] * 100)),
                int(round(r["high"] * 100)),
                int(round(r["low"] * 100)),
                int(round(r["close"] * 100)),
                float(r["amount"]),
                int(r["volume"]),
                0,  # reserved
            ))
    return path


def _merge_bars(local_records: list[dict], server_records: list[dict]) -> list[dict]:
    """按日期合并去重 (重叠日期服务器胜出), 日期升序。"""
    merged = {r["date"]: r for r in local_records}
    merged.update({r["date"]: r for r in server_records})
    return [merged[d] for d in sorted(merged)]


# ============================================================
# 双源数据验证
# ============================================================

def validate_bars(local_records: list[dict], server_records: list[dict],
                  tol: float = _PRICE_TOL) -> dict:
    """本地 vipdoc vs 服务器数据的重叠日期 OHLC 一致性验证。"""
    if not local_records or not server_records:
        return {
            "checked": 0, "consistent": None,
            "local_count": len(local_records), "server_count": len(server_records),
            "note": "单侧数据为空, 无法交叉验证",
        }

    lm = {r["date"]: r for r in local_records}
    sm = {r["date"]: r for r in server_records}
    common = sorted(set(lm) & set(sm))

    mismatches = []
    max_diff = 0.0
    for d in common:
        l, s = lm[d], sm[d]
        diffs = {k: abs(l[k] - s[k]) for k in _OHLC}
        worst_field = max(diffs, key=diffs.get)
        worst = diffs[worst_field]
        max_diff = max(max_diff, worst)
        if worst > tol:
            mismatches.append({
                "date": d, "field": worst_field, "max_abs_diff": round(worst, 4),
                "local": {k: l[k] for k in _OHLC},
                "server": {k: s[k] for k in _OHLC},
            })

    return {
        "checked": len(common),
        "local_count": len(local_records), "server_count": len(server_records),
        "local_only_dates": len(lm) - len(common),
        "server_only_dates": len(sm) - len(common),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:20],  # 最多展示 20 条
        "max_abs_diff": round(max_diff, 4),
        "tolerance": tol,
        "consistent": len(mismatches) == 0,
    }


# ============================================================
# 混合客户端
# ============================================================

class HybridClient:
    """本地 vipdoc 优先、服务器补缺的混合数据客户端。

    Parameters
    ----------
    vipdoc_dir : str | Path | None
        vipdoc 目录; None 时自动探测 (环境变量 TDXRS_VIPDOC / 常见路径)。
        探测失败则为纯服务器模式。
    timeout : float
        服务器连接超时 (秒)。
    client : object | None
        注入的服务器客户端 (需提供 get_security_bars), 主要用于测试。
    servers : list[(name, ip, port)] | None
        自定义服务器池。None 用默认 PRIMARY (连接失败时 Rust 层自动兜底
        全量 ALL_KNOWN_SERVERS); 传入 tdxrs.ALL_KNOWN_SERVERS 可把全部
        101 个 IP 注入池子, connect_to_any 按优先列表+全量顺序遍历。
    """

    def __init__(self, vipdoc_dir=None, timeout: float = 5.0, client=None,
                 servers=None):
        self.timeout = timeout
        if vipdoc_dir is not None:
            self.vipdoc_dir = Path(vipdoc_dir)
        else:
            self.vipdoc_dir = locate_vipdoc()
        self._client = client
        self._servers = list(servers) if servers else None
        self._last_server_error = None

    # ---------- 服务器 ----------

    def _get_client(self):
        if self._client is None:
            c = TdxHqClient()
            # 自定义 IP 池注入 (全量池/筛查结果均可)
            if self._servers:
                try:
                    c.set_servers([(n, ip, p) for n, ip, p in self._servers])
                except Exception:
                    pass
            ok = False
            # 优先用筛查缓存的最优服务器 (24h 内有效)
            try:
                from tdxrs.server_health import best_server

                best = best_server()
                if best:
                    ok = c.connect(best[0], best[1], self.timeout)
            except Exception:
                pass
            if not ok:
                ok = c.connect_to_any(self.timeout)
            if not ok:
                raise ConnectionError("无法连接任何行情服务器")
            self._client = c
        return self._client

    def _fetch_server_bars(self, code: str, market: int, count: int, fq: int = 0) -> list[dict]:
        """服务器日K (失败/返空都返回 [], 错误信息记入 _last_server_error)。"""
        self._last_server_error = None
        try:
            client = self._get_client()
            bars = client.get_security_bars(4, market, code, 0, count, fq)
            return [_normalize_server(b) for b in (bars or [])]
        except Exception as e:
            self._last_server_error = str(e)
            return []

    # ---------- 主入口 ----------

    def get_daily_bars(self, code: str, count: int = 800, market: int | None = None,
                       fq: int = 0, persist: bool = True, validate: bool = True) -> dict:
        """本地优先获取日K。

        Returns
        -------
        dict: {code, market, count, fq, source, bars, local_count,
               server_count, validation, vipdoc_dir, path, server_error}
        """
        market = market if market is not None else market_of(code)
        result = {
            "code": code, "market": market, "count": count, "fq": fq,
            "source": None, "bars": [],
            "local_count": 0, "server_count": 0,
            "validation": None,
            "vipdoc_dir": str(self.vipdoc_dir) if self.vipdoc_dir else None,
            "path": None, "server_error": None,
        }

        # 复权数据: 本地 vipdoc 为未复权, 直接走服务器
        if fq != 0:
            bars = self._fetch_server_bars(code, market, count, fq)
            result.update(source="server(fq)", bars=bars, server_count=len(bars),
                          server_error=self._last_server_error)
            return result

        # ---------- 本地读取 ----------
        local: list[dict] = []
        path = local_day_path(self.vipdoc_dir, code, market) if self.vipdoc_dir else None
        result["path"] = str(path) if path else None

        if path is not None and path.exists():
            # ---- 快路径: 本地条数足够 -> 只读尾部 count 条 ----
            # 零语义变化依据: 现状代码在 len(local) >= count 分支里只用了 local[-count:],
            # 前面 (total - count) 条的解析结果从未被使用。
            # 边界保持与旧实现一致: count <= 0 或文件非 32 整数倍时退回慢路径
            # (旧实现 count=0 会返回全部记录; 非 32 倍数会在解析时报错 -> local=[])。
            if count > 0:
                try:
                    size = path.stat().st_size
                    total = size // RECORD_SIZE
                    if size % RECORD_SIZE == 0 and total >= count:
                        m = read_tail(path, count)
                        if m.n >= count:
                            result["local_count"] = total
                            result.update(source="local", bars=m.to_bars())
                            return result
                except Exception:
                    pass
            try:
                local = read_local_day(path)
            except Exception:
                local = []
        result["local_count"] = len(local)

        # ---------- 本地足够: 纯本地, 不碰网络 ----------
        if len(local) >= count:
            result.update(source="local", bars=local[-count:])
            return result

        # ---------- 服务器补缺 ----------
        server = self._fetch_server_bars(code, market, count, 0)
        result["server_count"] = len(server)
        result["server_error"] = self._last_server_error

        if not server:
            # 服务器不可用/返空: 尽力返回本地
            result.update(source="local" if local else "server",
                          bars=local[-count:] if local else [])
            return result

        # 双源验证 (重叠日期)
        if local and validate:
            result["validation"] = validate_bars(local, server)

        merged = _merge_bars(local, server)
        if persist and path is not None:
            try:
                write_local_day(path, merged)
            except Exception as e:
                result["persist_error"] = str(e)
        result.update(source="local+server" if local else "server",
                      bars=merged[-count:])
        return result

    def validate(self, code: str, count: int = 800, market: int | None = None) -> dict:
        """独立双源验证报告: 本地 vipdoc vs 服务器数据。"""
        market = market if market is not None else market_of(code)
        local: list[dict] = []
        if self.vipdoc_dir:
            path = local_day_path(self.vipdoc_dir, code, market)
            if path.exists():
                try:
                    local = read_local_day(path)
                except Exception:
                    pass
        server = self._fetch_server_bars(code, market, count, 0)
        report = validate_bars(local, server)
        report.update(
            code=code, market=market,
            local_last_date=local[-1]["date"] if local else None,
            server_last_date=server[-1]["date"] if server else None,
            server_error=self._last_server_error,
            vipdoc_dir=str(self.vipdoc_dir) if self.vipdoc_dir else None,
        )
        return report


# ============================================================
# 模块级便捷接口
# ============================================================

_default_client: HybridClient | None = None


def get_daily_bars(code: str, count: int = 800, market: int | None = None,
                   fq: int = 0, persist: bool = True) -> dict:
    """便捷接口 (进程内复用默认 HybridClient)。"""
    global _default_client
    if _default_client is None:
        _default_client = HybridClient()
    return _default_client.get_daily_bars(code, count=count, market=market,
                                          fq=fq, persist=persist)


if __name__ == "__main__":
    hc = HybridClient()
    r = hc.get_daily_bars("600519", count=30)
    print(f"source={r['source']}  local={r['local_count']}  server={r['server_count']}")
    if r["validation"]:
        v = r["validation"]
        print(f"validation: checked={v['checked']} consistent={v['consistent']}")
    for b in r["bars"][-5:]:
        print(b)
