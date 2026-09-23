"""hybrid 混合数据层单元测试 (无网络依赖)

运行: python -m pytest tests/test_hybrid.py -q
      或 python tests/test_hybrid.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tdxrs.hybrid import (  # noqa: E402
    HybridClient,
    _merge_bars,
    local_day_path,
    market_of,
    read_local_day,
    validate_bars,
    write_local_day,
)


# ---------- 测试工具 ----------

def _mk_bar(date, close=10.0, open_=None, high=None, low=None, volume=1000, amount=1_000_000.0):
    open_ = open_ if open_ is not None else close
    high = high if high is not None else max(open_, close) * 1.01
    low = low if low is not None else min(open_, close) * 0.99
    return {"date": date, "open": round(open_, 2), "high": round(high, 2),
            "low": round(low, 2), "close": round(close, 2),
            "volume": volume, "amount": amount}


def _mk_server_bar(date, close=10.0, **kw):
    """服务器返回格式 (datetime/vol) -> 由 _normalize_server 转换。"""
    b = _mk_bar(date, close=close, **kw)
    return {"datetime": f"{date} 15:00", "open": b["open"], "high": b["high"],
            "low": b["low"], "close": b["close"], "vol": b["volume"], "amount": b["amount"]}


class FakeClient:
    """注入用假服务器客户端"""

    def __init__(self, bars=None):
        self.bars = bars or []
        self.calls = []

    def get_security_bars(self, category, market, code, start, count, fq):
        self.calls.append((category, market, code, start, count, fq))
        return self.bars


def _dates(n, start=(2026, 1, 1)):
    """生成 n 个连续日期字符串 (跳过周末不必要, 日期唯一即可)。"""
    from datetime import date, timedelta

    d0 = date(*start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


# ---------- 市场识别 ----------

def test_market_of():
    assert market_of("600519") == 1  # SH
    assert market_of("510300") == 1  # SH 基金
    assert market_of("000001") == 0  # SZ
    assert market_of("300750") == 0  # SZ 创业板
    assert market_of("430047") == 2  # BJ
    assert market_of("920001") == 2  # BJ 新股


def test_local_day_path():
    p = local_day_path(r"C:\vipdoc", "600519")
    assert str(p).replace("\\", "/") == "C:/vipdoc/sh/lday/sh600519.day"
    p = local_day_path(r"C:\vipdoc", "000001")
    assert str(p).replace("\\", "/") == "C:/vipdoc/sz/lday/sz000001.day"


# ---------- .day 读写回环 ----------

def test_day_roundtrip(tmp_path):
    bars = [_mk_bar("2026-09-18", close=10.55),
            _mk_bar("2026-09-21", close=10.80),
            _mk_bar("2026-09-22", close=10.62)]
    path = tmp_path / "sh" / "lday" / "sh600519.day"
    write_local_day(path, bars)
    assert path.exists()

    loaded = read_local_day(path)
    assert len(loaded) == 3
    assert loaded[0]["date"] == "2026-09-18"
    for got, want in zip(loaded, bars):
        for k in ("open", "high", "low", "close"):
            assert abs(got[k] - want[k]) < 0.005, f"{k}: {got[k]} != {want[k]}"
        assert got["volume"] == want["volume"]


# ---------- 本地优先 ----------

def test_local_sufficient_no_network(tmp_path):
    """本地条数足够时不碰服务器"""
    dates = _dates(300)
    bars = [_mk_bar(d, close=10.0 + i * 0.01) for i, d in enumerate(dates)]
    write_local_day(local_day_path(tmp_path, "600519"), bars)

    fake = FakeClient()
    hc = HybridClient(vipdoc_dir=tmp_path, client=fake)
    r = hc.get_daily_bars("600519", count=100)

    assert r["source"] == "local"
    assert len(r["bars"]) == 100
    assert r["local_count"] == 300
    assert fake.calls == []  # 完全没碰服务器


def test_server_fallback_when_missing(tmp_path):
    """本地缺失 -> 服务器拉取并回写本地"""
    dates = _dates(50)
    fake = FakeClient([_mk_server_bar(d, close=10.0) for d in dates])

    hc = HybridClient(vipdoc_dir=tmp_path, client=fake)
    r = hc.get_daily_bars("600519", count=50)

    assert r["source"] == "server"
    assert r["local_count"] == 0
    assert r["server_count"] == 50
    assert len(r["bars"]) == 50
    assert fake.calls and fake.calls[0][5] == 0  # fq=0
    # 已回写本地
    assert local_day_path(tmp_path, "600519").exists()
    assert len(read_local_day(local_day_path(tmp_path, "600519"))) == 50


def test_merge_local_and_server(tmp_path):
    """本地不全 -> 服务器补缺, 按日期合并去重 + 双源验证"""
    all_dates = _dates(40)
    local_dates, overlap_dates, server_dates = all_dates[:20], all_dates[:30], all_dates[10:]

    # 本地 20 条; 服务器 30 条 (含 10 天重叠, 重叠值一致)
    local_bars = [_mk_bar(d, close=10.0) for d in local_dates]
    write_local_day(local_day_path(tmp_path, "600519"), local_bars)
    fake = FakeClient([_mk_server_bar(d, close=10.0) for d in server_dates])

    hc = HybridClient(vipdoc_dir=tmp_path, client=fake)
    r = hc.get_daily_bars("600519", count=40)

    assert r["source"] == "local+server"
    assert r["local_count"] == 20
    assert r["server_count"] == 30
    assert len(r["bars"]) == 40  # 40 天去重后
    assert r["bars"][0]["date"] == all_dates[0]
    assert r["bars"][-1]["date"] == all_dates[-1]
    # 验证报告: 20 天重叠 (本地20 ∩ 服务器30 = all_dates[10:20] = 10 天)
    v = r["validation"]
    assert v["checked"] == 10
    assert v["consistent"] is True
    # 回写后本地应有 40 条
    assert len(read_local_day(local_day_path(tmp_path, "600519"))) == 40


def test_server_empty_falls_back_to_local(tmp_path):
    """服务器返空 (当前真实状态) -> 尽力返回本地"""
    local_bars = [_mk_bar(d, close=10.0) for d in _dates(30)]
    write_local_day(local_day_path(tmp_path, "600519"), local_bars)

    hc = HybridClient(vipdoc_dir=tmp_path, client=FakeClient(bars=[]))
    r = hc.get_daily_bars("600519", count=100)

    assert r["source"] == "local"
    assert len(r["bars"]) == 30  # 只有本地 30 条


def test_fq_goes_to_server(tmp_path):
    """fq != 0 直接走服务器"""
    fake = FakeClient([_mk_server_bar(d, close=10.0) for d in _dates(10)])
    hc = HybridClient(vipdoc_dir=tmp_path, client=fake)
    r = hc.get_daily_bars("600519", count=10, fq=1)

    assert r["source"] == "server(fq)"
    assert fake.calls[0][5] == 1


# ---------- 验证 ----------

def test_validate_detects_mismatch(tmp_path):
    """重叠日期价格不一致 -> 报告 mismatch"""
    dates = _dates(10)
    # 显式固定 OHLC, 两端仅 close 相差 0.55
    # (避免 _mk_bar 从 close 派生 high/low 引入额外差异字段)
    write_local_day(local_day_path(tmp_path, "600519"),
                    [_mk_bar(d, close=10.00, open_=10.00, high=10.80, low=9.50)
                     for d in dates])
    # 服务器第 5 天 close 差 0.55
    server = [_mk_server_bar(d, close=10.55 if d == dates[4] else 10.00,
                             open_=10.00, high=10.80, low=9.50)
              for d in dates]

    hc = HybridClient(vipdoc_dir=tmp_path, client=FakeClient(server))
    # 请求 20 条 > 本地 10 条 -> 触发服务器补缺 + 双源验证
    r = hc.get_daily_bars("600519", count=20)

    v = r["validation"]
    assert v["checked"] == 10
    assert v["consistent"] is False
    assert v["mismatch_count"] == 1
    assert v["mismatches"][0]["date"] == dates[4]
    assert abs(v["mismatches"][0]["max_abs_diff"] - 0.55) < 0.01


def test_validate_bars_empty_side():
    r = validate_bars([], [_mk_bar("2026-09-18")])
    assert r["checked"] == 0
    assert r["consistent"] is None
    assert "无法交叉验证" in r["note"]


def test_merge_dedup_server_wins():
    local = [_mk_bar("2026-09-18", close=10.0)]
    server = [_mk_bar("2026-09-18", close=10.5), _mk_bar("2026-09-19", close=10.6)]
    merged = _merge_bars(local, server)
    assert len(merged) == 2
    assert merged[0]["close"] == 10.5  # 重叠日期服务器胜出
    assert merged[1]["close"] == 10.6


if __name__ == "__main__":
    import inspect
    import tempfile

    fails = 0
    for name, fn in sorted(
        (n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)
    ):
        kwargs = {}
        if "tmp_path" in inspect.signature(fn).parameters:
            with tempfile.TemporaryDirectory() as td:
                kwargs["tmp_path"] = Path(td)
        try:
            fn(**kwargs)
            print(f"PASS {name}")
        except AssertionError as e:
            fails += 1
            print(f"FAIL {name}: {e}")
    print(f"\n{'ALL PASS' if fails == 0 else f'{fails} FAILED'}")
    sys.exit(1 if fails else 0)
