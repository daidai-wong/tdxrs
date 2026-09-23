"""DataFrame 列式直建 dtype 断言 (阶段 4 / B4, 离线无网络, 无 pytest 依赖)

验证 numpy 列式直建后:
- 列名与列序与 dict-of-lists 旧实现完全一致
- 数值列 dtype: float64 (价格/量/额) / int64 (年月日)
- 字符串列: object (date)
- 数值正确性: 回读值与写入值一致

运行: python tests/test_dataframe_dtype.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from tdxrs import DailyBarReader
from tdxrs.hybrid import write_local_day

EXPECTED_COLS = ["date", "year", "month", "day", "open", "high", "low",
                 "close", "volume", "amount"]


def make_day_file(tmp: Path):
    """合成 300 条日线 .day 文件, 返回 (path, bars)"""
    bars = []
    y, m, d = 2025, 1, 2
    for i in range(300):
        close = 10.0 + (i % 97) * 0.13
        bars.append({
            "date": f"{y:04d}-{m:02d}-{d:02d}",
            "open": round(close - 0.05, 2), "high": round(close + 0.21, 2),
            "low": round(close - 0.18, 2), "close": round(close, 2),
            "volume": 10000 + i, "amount": 1_000_000.0 + i * 37.5,
        })
        d += 1
        if d > 28:
            d, m = 1, m + 1
            if m > 12:
                m, y = 1, y + 1
    p = write_local_day(tmp / "sh" / "lday" / "sh600000.day", bars)
    return p, bars


def new_tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="tdxrs_df_test_"))


def test_df_columns_and_dtypes():
    path, bars = make_day_file(new_tmp())
    df = DailyBarReader().to_dataframe_file(str(path))

    assert isinstance(df, pd.DataFrame), "type"
    assert list(df.columns) == EXPECTED_COLS, f"columns: {list(df.columns)}"
    assert len(df) == len(bars), "len"

    # 数值列 dtype (列式直建的核心收益: dtype 语义正确)
    for col in ("open", "high", "low", "close", "volume", "amount"):
        assert df[col].dtype == np.float64, f"{col} dtype={df[col].dtype}"
    for col in ("year", "month", "day"):
        assert df[col].dtype == np.int64, f"{col} dtype={df[col].dtype}"
    # 字符串列 (pandas 2.x = object; pandas 3.0+ 默认 StringDtype, PDEP-14)
    date_dtype = df["date"].dtype
    assert date_dtype == object or pd.api.types.is_string_dtype(df["date"]), \
        f"date dtype={date_dtype}"


def test_df_values_roundtrip():
    path, bars = make_day_file(new_tmp())
    df = DailyBarReader().to_dataframe_file(str(path))

    # .day 价格为 ×100 整数存储, 回读精度 0.01
    for i in (0, 1, 149, 299):
        assert abs(df["close"].iloc[i] - bars[i]["close"]) < 0.005, f"close[{i}]"
        assert abs(df["open"].iloc[i] - bars[i]["open"]) < 0.005, f"open[{i}]"
        assert abs(df["high"].iloc[i] - bars[i]["high"]) < 0.005, f"high[{i}]"
        assert abs(df["low"].iloc[i] - bars[i]["low"]) < 0.005, f"low[{i}]"
        assert abs(df["volume"].iloc[i] - bars[i]["volume"]) < 0.5, f"volume[{i}]"
        assert df["date"].iloc[i] == bars[i]["date"], f"date[{i}]"


def test_df_parse_bytes():
    """parse bytes -> DataFrame 同样走列式直建"""
    path, bars = make_day_file(new_tmp())
    data = path.read_bytes()
    assert len(data) == len(bars) * 32, "record size"

    df = DailyBarReader().to_dataframe(data)
    assert isinstance(df, pd.DataFrame), "type"
    assert list(df.columns) == EXPECTED_COLS, "columns"
    assert len(df) == len(bars), "len"
    assert df["open"].dtype == np.float64, "open dtype"
    assert df["year"].dtype == np.int64, "year dtype"


def test_df_memory_layout():
    """numpy 列的内存连续性 (零拷贝采纳的基础)"""
    path, _ = make_day_file(new_tmp())
    df = DailyBarReader().to_dataframe_file(str(path))
    arr = df["close"].to_numpy()
    assert arr.dtype == np.float64, "dtype"
    assert arr.flags["C_CONTIGUOUS"], "contiguous"


if __name__ == "__main__":
    failures = 0
    for fn in (test_df_columns_and_dtypes, test_df_values_roundtrip,
               test_df_parse_bytes, test_df_memory_layout):
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {e}")
    print("\nALL PASS" if failures == 0 else f"\n{failures} FAILED")
    sys.exit(1 if failures else 0)
