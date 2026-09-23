"""server_health 模块单元测试 (无网络依赖)

运行: python -m pytest tests/test_server_health.py -q
      或 python tests/test_server_health.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tdxrs.server_health import (  # noqa: E402
    _mark_missing,
    _sort_results,
    compute_score,
    grade_of,
    load_report,
    save_report,
)


# ---------- 评分 ----------

def test_score_basic_range():
    s = compute_score(10.0, 20.0, 50.0)
    assert 0.0 <= s <= 100.0
    # 加权延时 = 0.4*10 + 0.3*20 + 0.3*50 = 25ms -> 扣 2.5 分
    assert abs(s - 97.5) < 0.01


def test_score_formula_with_data():
    # 精确验证公式 (用大延时避免 +35 加分触发 100 封顶):
    # base_lat = 0.4*400+0.3*500+0.3*500 = 460
    # mixed    = 0.7*460 + 0.3*300       = 412 -> 扣 41.2
    # score    = 100 - 41.2 + 25 + 10    = 93.8
    s = compute_score(400.0, 500.0, 500.0, data_ms=300.0, data_ok=True, bars_ok=True)
    assert abs(s - 93.8) < 0.01


def test_score_data_plane_dominates():
    # 现实差分场景: 数据可用的服务器 应优于 延时更低但返空的服务器
    fast_dead = compute_score(5.0, 5.0, 10.0, data_ok=False)          # 99.3
    alive = compute_score(60.0, 80.0, 150.0, data_ms=80.0,
                          data_ok=True, bars_ok=True)                 # 封顶 100.0
    assert alive > fast_dead
    assert alive == 100.0


def test_score_clamped():
    # 超高延时不产生负分
    assert compute_score(5000.0, 5000.0, 5000.0) >= 0.0


# ---------- 级别 ----------

def test_grade_boundaries():
    assert grade_of(85.0) == ("A", "优秀")
    assert grade_of(84.9) == ("B", "良好")
    assert grade_of(70.0) == ("B", "良好")
    assert grade_of(69.9) == ("C", "可用")
    assert grade_of(55.0) == ("C", "可用")
    assert grade_of(10.0) == ("D", "勉强")
    assert grade_of(0.0) == ("D", "勉强")  # 失联 F 由 screen_servers 显式标注


# ---------- 排序 ----------

def test_sort_score_desc_then_api_asc():
    results = [
        {"score": 80.0, "api_ms": 50.0},
        {"score": 90.0, "api_ms": 99.0},
        {"score": 90.0, "api_ms": 20.0},
        {"score": 0.0, "api_ms": None},
    ]
    out = _sort_results(results)
    assert [r["score"] for r in out] == [90.0, 90.0, 80.0, 0.0]
    assert out[0]["api_ms"] == 20.0 and out[1]["api_ms"] == 99.0


# ---------- 失联标记 ----------

def test_mark_missing():
    results = [
        {"name": "A", "ip": "1.1.1.1", "port": 7709, "score": 90.0, "grade": "A"},
    ]
    universe = [
        ("A", "1.1.1.1", 7709),
        ("B", "2.2.2.2", 7709),
    ]
    out = _mark_missing(results, universe)
    assert len(out) == 2
    dead = [r for r in out if r["ip"] == "2.2.2.2"][0]
    assert dead["grade"] == "F" and dead["score"] == 0.0
    assert "失联" in dead["status"]


# ---------- 缓存 ----------

def test_cache_roundtrip(tmp_path):
    results = [{"name": "X", "ip": "3.3.3.3", "port": 7709, "score": 88.0,
                "grade": "A", "grade_label": "优秀", "status": "数据OK"}]
    path = tmp_path / "servers.json"
    save_report(results, path)
    loaded = load_report(path)
    assert loaded["count"] == 1
    assert loaded["results"][0]["ip"] == "3.3.3.3"
    assert "timestamp" in loaded
    # 原始 JSON 可解析
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["results"][0]["name"] == "X"


def test_load_report_missing(tmp_path):
    assert load_report(tmp_path / "nope.json") is None


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(
        (n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)
    ):
        kwargs = {}
        import inspect

        if "tmp_path" in inspect.signature(fn).parameters:
            import tempfile

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
