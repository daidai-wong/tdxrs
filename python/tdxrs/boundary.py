# -*- coding: utf-8 -*-
"""Arrow / Parquet / DuckDB 边界互操作 —— **只做边界, 不进热路径**。

为什么 Arrow 不能进热路径
-------------------------
vipdoc 记录是 32 字节**行主序**定长结构, 取一列得到的是**跨步视图**
(stride=32, itemsize=4), 而 Arrow / Parquet 要求每个 buffer 连续 ——
所以「numpy -> Arrow」必然付一次拷贝。实测 (100 万行 / 30.5 MB, best-of-5):

    ================================================  ==========  ==========
    转换                                               耗时        备注
    ================================================  ==========  ==========
    np.ascontiguousarray(raw.T)  转置全部 8 列          16.12 ms    1866 MB/s
    pa.array(raw[:, 4])          按需建 1 个跨步列       1.76 ms    **便宜 9x**
    pa.array(raw[:, i]) x 8      按需建全部 8 列        12.09 ms    比转置还快 25%
    pa.array(连续列)                                    0.002 ms    **零拷贝**
    pl.from_numpy(连续 2D)                              0.03 ms    零拷贝
    pd.DataFrame(8 个连续列)                            5.72 ms    比 Arrow 贵
    ================================================  ==========  ==========

两条直接结论:
  1. **按需建列, 不要无条件整体转置**。只要 1~2 列时比转置便宜约 9x;
     要全部 8 列时逐列仍比转置快 25% (转置是 cache 不友好的 tiled 拷贝)。
     确有「一块连续内存」需求时再传 ``shared_buffer=True``。
  2. 这次拷贝**不是浪费** —— 列存格式本来就必须是列式布局。一次转置
     (≈16 ms/百万行) 换来的是「落盘一次、之后不再重扫 vipdoc」:

        重扫 vipdoc  scan_daily           173.6 ms   (2000 文件 / 212 万行 / 64.7 MiB)
        建缓存       + Arrow + Feather    264.8 ms   一次性
        重开         read_table            21.8 ms   比重扫快 8.0x
        重开+物化     + to_pandas           34.7 ms   比重扫快 5.0x

    即缓存重开 2 次即回本 (0.2648 + n*0.0218 < n*0.1736 -> n >= 2)。
    但注意 ``memory_map=True`` **不是零拷贝**, 详见 :func:`open_feather`。

边界陷阱 (实测)
---------------
* ``Table.to_pandas()`` **默认会再拷一遍全部列** (pandas BlockManager 会把同
  dtype 的多列合并成一个二维 block)。必须用 ``split_blocks=True`` 或
  ``types_mapper=pd.ArrowDtype`` 才是零拷贝 —— 本模块的 :func:`to_pandas`
  默认就带 ``split_blocks=True``。
* ``feather.read_table(..., memory_map=True)`` **不是零拷贝** (见 :func:`open_feather`)。
* ``feather.write_feather`` 默认按 65536 行切 batch, 写慢 1.3x / 读慢 1.7x ——
  本模块默认写成单批 (:func:`write_feather`)。
* 价格列在 Arrow 里保持 ``uint32`` 原值 (×100), **不是** f64。
  想要 f64 价格请显式传 ``price_scale=True`` (会多一次 8B/行 的拷贝);
  保持整数原值是精确的, 而且省一半内存。
* ``.lc1`` 的第 0 个 32 位槽位是 ``date(u16)+time(u16)`` 打包, 不能当成
  单个 u32 日期用 —— 本模块按 dtype 字段名取列, 已正确处理。
* ``pa.array(x).cast(pa.float32())`` 是**数值转换**(会拷贝), f32 重解释要用
  ``x.view(np.float32)``。

用法
----
    from tdxrs import local, boundary

    tb = local.read_daily(path).to_arrow()            # 只建需要的列
    tb = local.scan_daily(dir).to_arrow(columns=["close","volume"], with_code=True)

    boundary.write_feather(tb, "cache.feather")       # 一次性建列存缓存
    tb2 = boundary.open_feather("cache.feather")      # 之后 mmap 近零成本
    df = boundary.to_pandas(tb2)                      # split_blocks -> 零拷贝

pyarrow / polars / duckdb 都是**可选依赖**, 本模块自身不 import 它们
(只在函数内部 import), 缺依赖时给出明确报错。
"""
from __future__ import annotations

import numpy as np

__all__ = [
    "HAVE_PYARROW", "import_pyarrow",
    "matrix_to_arrow", "panel_to_arrow", "to_pandas",
    "write_feather", "open_feather", "write_parquet", "open_parquet",
    "register_duckdb", "column_names",
]

# .day 的 8 个槽位 -> 逻辑列名 (与磁盘字段一一对应)
DAY_COLUMNS = ("date", "open", "high", "low", "close", "amount", "volume",
               "reserved")
# .lc 的 8 个槽位: 第 0 槽是 date(u16)+time(u16) 打包, 拆成两列
LC_COLUMNS = ("date", "time", "open", "high", "low", "close", "amount",
              "volume")
# 价格类列 (.day 为 u32 x100, .lc 已是 f32 实际价)
PRICE_COLUMNS = ("open", "high", "low", "close")
# 整数金额/量的列
_INT_COLUMNS = ("date", "open", "high", "low", "close", "volume", "reserved")
_INT_COLUMNS_LC = ("date", "time", "open", "high", "low", "close", "volume")


def import_pyarrow():
    """惰性导入 pyarrow, 缺依赖时给出可操作的报错。"""
    try:
        import pyarrow as pa
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "该功能需要 pyarrow: pip install pyarrow\n"
            "(tdxrs 核心读取路径不依赖 pyarrow, 仅 Arrow/Parquet 边界需要)"
        ) from e
    return pa


def _has_pyarrow() -> bool:
    try:
        import pyarrow  # noqa: F401
        return True
    except ImportError:
        return False


HAVE_PYARROW = _has_pyarrow()


def column_names(kind: str = "day") -> tuple:
    """该种类可用的逻辑列名。"""
    return LC_COLUMNS if kind == "lc" else DAY_COLUMNS


def _select(columns, kind: str, extra: tuple = ()) -> list:
    available = column_names(kind)
    if columns is None:
        cols = [c for c in available if c != "reserved"]
    elif isinstance(columns, str):
        cols = [columns]
    else:
        cols = list(columns)
    bad = [c for c in cols if c not in available]
    if bad:
        raise ValueError(f"未知列 {bad}; 可选 {list(available)}")
    for e in extra:
        if e not in cols:
            cols.append(e)
    return cols


# ============================================================
# 核心: 记录视图 -> Arrow Array (每列一次拷贝, 不可避免)
# ============================================================
def _arrow_column(arr_or_view, kind: str, name: str, price_scale: float,
                  group: dict):
    """把一个字段视图转成 Arrow Array 并记入共享 buffer 分组。

    ``group`` 用于统计: 本模块默认每列独立 buffer (实测比整体转置便宜 25%);
    传 shared_buffer=True 时改走 :func:`matrix_to_arrow` 的转置分支。
    """
    pa = import_pyarrow()

    if name == "amount":
        # f32 重解释: 必须用 .view 而非 cast (cast 是数值转换, 会多做一次拷贝)
        v = arr_or_view.view(np.float32) if arr_or_view.dtype != np.float32 \
            else arr_or_view
        return pa.array(v)
    if name in PRICE_COLUMNS and price_scale and kind == "day":
        return pa.array(arr_or_view.astype(np.float64) * price_scale)
    return pa.array(arr_or_view)


def _fields_of(m):
    """Matrix -> {列名: 原始字段视图} (按 dtype 字段取, 正确处理 .lc 的打包槽)。"""
    raw = m.raw
    names = raw.dtype.names
    if m.kind == "lc":
        out = {"date": raw["date"], "time": raw["time"],
               "open": raw["open"], "high": raw["high"], "low": raw["low"],
               "close": raw["close"], "amount": raw["amount"],
               "volume": raw["volume"]}
    else:
        out = {"date": raw["date"], "open": raw["open"], "high": raw["high"],
               "low": raw["low"], "close": raw["close"],
               "amount": raw["amount"], "volume": raw["volume"],
               "reserved": raw["reserved"]}
    assert set(out) <= set(names) | {"date", "time"}, out.keys()
    return out


def matrix_to_arrow(m, columns=None, code=None, with_code=None,
                    date_as: str = "raw", price_scale: bool = False,
                    shared_buffer: bool = False, with_validity: bool = False):
    """Matrix -> pyarrow.Table。

    Parameters
    ----------
    columns : str | list | None
        需要哪些列 (None = 除 ``reserved`` 外全部)。**按需建列**是这里最重要的
        性能开关: 只要 1 列比整体转置便宜约 9x。
    code : str | None
        单文件场景的证券代码; 写入为 dictionary 列 (单个字典值)。
    with_code : bool | None
        是否输出 ``code`` 列; None 时由 ``code`` 是否给出决定。
    date_as : {"raw", "date32"}
        ``raw`` 直接用文件里的整数 (u32 YYYYMMDD, **零额外拷贝**);
        ``date32`` 额外算一列真正的 Arrow date32 (多 4B/行)。
    price_scale : bool
        True 时把 ``.day`` 的 OHLC 转成 f64 实际价格 (多一次 8B/行 的拷贝)。
        默认 False: 保持 uint32 原值 (×100) 是精确且省内存的。
    shared_buffer : bool
        True 时改用 ``np.ascontiguousarray(raw2d.T)`` 整体转置, 8 列共享一块
        连续内存 (适合把整段记录做成单个 IPC buffer)。
        默认 False —— 实测逐列建 buffer 比转置快 25%, 且 1~2 列时便宜 9x。
    """
    pa = import_pyarrow()
    if date_as not in ("raw", "date32"):
        raise ValueError(f"date_as 只支持 raw / date32, 得到 {date_as!r}")

    fields = _fields_of(m)
    cols = _select(columns, m.kind)
    extra = (["date32"] if date_as == "date32" else []) + \
            (["code"] if (with_code or code is not None) else [])
    need = _select(cols, m.kind, extra)

    out = {}
    if shared_buffer:
        # 整体转置: 8 列共享同一块 (8, n) 连续 buffer, pa.array 全部零拷贝
        t = np.ascontiguousarray(m.raw2d.T)
        tf = t.view(np.float32)
        slot = {"date": 0, "open": 1, "high": 2, "low": 3, "close": 4,
                "amount": 5, "volume": 6, "reserved": 7}
        if m.kind == "lc":       # .lc 的槽 0 是 date+time 打包, 转置路径不适用
            raise ValueError("shared_buffer=True 不支持 .lc (第 0 槽是打包的 "
                             "date+time); 请用默认的逐列模式")
        for nm in need:
            if nm in ("date32", "code"):
                continue
            out[nm] = pa.array(tf[slot[nm]] if nm == "amount" else t[slot[nm]])
    else:
        for nm in need:
            if nm in ("date32", "code"):
                continue
            out[nm] = _arrow_column(fields[nm], m.kind, nm,
                                    m.coefficient if price_scale else 0.0, {})

    if date_as == "date32":
        out["date32"] = pa.array(m.dates)
    if "code" in need:
        c = str(code) if code is not None else (m.code or "")
        out["code"] = pa.DictionaryArray.from_arrays(
            pa.array(np.zeros(m.n, dtype=np.int32)), pa.array([c]))

    # 列顺序: date / date32 / 其余 / code
    order = [c for c in ("date", "date32") if c in out] + \
            [c for c in need if c in out and c not in ("date", "date32", "code")] + \
            (["code"] if "code" in out else [])
    return pa.table({k: out[k] for k in order})


def panel_to_arrow(panel, columns=None, with_code: bool = True,
                   date_as: str = "raw", price_scale: bool = False,
                   shared_buffer: bool = False):
    """DailyPanel -> pyarrow.Table (一次调用出整个市场的列存表)。

    ``code`` 为 dictionary 编码 (212 万行实测 6.3 MB, 字符串列 24.9 MB)。
    """
    pa = import_pyarrow()
    if date_as not in ("raw", "date32"):
        raise ValueError(f"date_as 只支持 raw / date32, 得到 {date_as!r}")

    m = panel.merged()
    fields = _fields_of(m)
    cols = _select(columns, panel.kind)
    need = _select(cols, panel.kind, ["date32"] if date_as == "date32" else [])
    coef = m.coefficient if price_scale else 0.0

    out = {}
    if shared_buffer:
        if panel.kind == "lc":
            raise ValueError("shared_buffer=True 不支持 .lc")
        t = np.ascontiguousarray(panel.raw2d.T)
        tf = t.view(np.float32)
        slot = {"date": 0, "open": 1, "high": 2, "low": 3, "close": 4,
                "amount": 5, "volume": 6, "reserved": 7}
        for nm in need:
            if nm == "date32":
                continue
            out[nm] = pa.array(tf[slot[nm]] if nm == "amount" else t[slot[nm]])
    else:
        for nm in need:
            if nm == "date32":
                continue
            out[nm] = _arrow_column(fields[nm], panel.kind, nm, coef, {})

    if date_as == "date32":
        out["date32"] = pa.array(m.dates)
    if with_code:
        out["code"] = pa.DictionaryArray.from_arrays(
            pa.array(panel.code_indices()),
            pa.array([str(c) for c in panel.codes]))

    order = [c for c in ("date", "date32") if c in out] + \
            [c for c in need if c in out and c not in ("date", "date32")] + \
            (["code"] if "code" in out else [])
    return pa.table({k: out[k] for k in order})


# ============================================================
# pandas 边界 (默认零拷贝)
# ============================================================
def to_pandas(table, zero_copy: bool = True, arrow_backed: bool = False):
    """Table -> DataFrame。

    ``zero_copy=True`` (默认) 传 ``split_blocks=True`` —— 实测这是唯一能避免
    pandas 把同 dtype 多列合并成二维 block 时再拷一遍的姿势。
    ``arrow_backed=True`` 则改用 ``types_mapper=pd.ArrowDtype`` (列保持 Arrow
    后端, 同样零拷贝, 适合继续做 Arrow 侧运算)。
    """
    if arrow_backed:
        import pandas as pd
        return table.to_pandas(types_mapper=pd.ArrowDtype)
    return table.to_pandas(split_blocks=bool(zero_copy))


# ============================================================
# 列存缓存 (一次性建, 之后 mmap 近零成本)
# ============================================================
def write_feather(table, path, compression: str = "lz4", chunksize: int = 0) -> str:
    """写 Feather (Arrow IPC)。lz4 实测 0.62x 原始大小。

    ``chunksize=0`` (默认) 写成**单个 record batch**。这一点很关键:
    pyarrow 默认会把表切成 65536 行一个 batch —— 212 万行 -> 33 个 batch,
    实测写慢 1.3x、读慢 1.7x, 而文件大小几乎不变:

        chunksize        写        全表读     单列读     batch 数
        None (默认)      37.1 ms   20.09 ms   6.92 ms    33
        691200           28.3 ms   12.53 ms   5.20 ms     4
        **0 (单批)**     28.7 ms   11.78 ms   5.12 ms     1
    """
    import pyarrow.feather as feather
    kw = {} if chunksize is None else {"chunksize": int(chunksize)}
    feather.write_feather(table, str(path), compression=compression, **kw)
    return str(path)


def open_feather(path, columns=None, memory_map: bool = True):
    """打开 Feather 缓存。

    .. warning::
       ``memory_map=True`` **并不等于零拷贝** —— 实测对同一 mmap 连续读两次,
       Arrow buffer 地址不同, 说明 pyarrow 仍把数据拷了出来
       (map 成本本身只有 0.26 ms, ``read_table`` 却要 ~98 ms / 188 MB,
       即 ~1.9 GB/s, 是真实的内存读)。所以不要把它描述成 zero-copy。

    真正的收益有两条:
      ① 免去重扫 vipdoc 的解析 -- 全市场 997.9 万行 98 ms vs ``scan_daily`` 869 ms;
      ② **列裁剪**: ``columns=['close']`` 实测 5.1 ms, 而全表 11.8 ms (2000 文件版本)。

    ``columns`` 非 None 时走列裁剪 (只碰需要的列区间)。
    """
    import pyarrow.feather as feather
    return feather.read_table(str(path), columns=columns, memory_map=memory_map)


def write_parquet(table, path, compression: str = "zstd") -> str:
    """写 Parquet。zstd 实测 0.58x 原始大小, 写入 ~535 ms/212 万行 (比 Feather 慢)。"""
    import pyarrow.parquet as pq
    pq.write_table(table, str(path), compression=compression)
    return str(path)


def open_parquet(path, columns=None):
    """读 Parquet (可只取部分列 —— 列存裁剪, 这是它相对 Feather 的唯一优势)。"""
    import pyarrow.parquet as pq
    return pq.read_table(str(path), columns=columns)


def register_duckdb(table, con=None, name: str = "t"):
    """把 Arrow 表注册进 DuckDB (零拷贝) 并返回 (con, name)。

    实测 212 万行: register 后聚合 44 ms、group by 41 ms。
    DuckDB 直读 Parquet 更好: ``scan`` 10 ms + 点查 14 ms (列裁剪 + 下推)。
    """
    try:
        import duckdb
    except ImportError as e:  # pragma: no cover
        raise ImportError("需要 duckdb: pip install duckdb") from e
    if con is None:
        con = duckdb.connect()
    con.register(name, table)
    return con, name
