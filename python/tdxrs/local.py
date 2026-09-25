# -*- coding: utf-8 -*-
"""tdxrs 本地 vipdoc 零拷贝读取器。

为什么是纯 Python + numpy 而不是 PyO3 扩展:
    .day / .lc1 都是 32 字节定长记录, 等价于一个 numpy dtype 声明。
    跨语言边界每过一次就搬一次数据, 所以搬运次数决定了性能上限:

        现状 to_dataframe_file   : 1 (fs::read) + 1 (Vec<Record>+String) + 1 (列式->numpy) = 3
        本模块 read_daily        : 1 (read_bytes) + 0 + 0 = 1
        本模块 read_daily_mmap   : 0 (按页缺页) + 0 + 0 = 0

    若由 Rust 返回 numpy 数组, 仍是 >=2 次 (Vec<u8> 读 + PyArray 构造)。
    因此内部热路径用 numpy 行主序视图, 真零拷贝。

文件格式 (官方 vipdoc):
    .day   <IIIIIfII>  32B  date(u32=YYYYMMDD 整数) OHLC(u32, 价格 x100) amount(f32) volume(u32) rsv(u32)
    .lc1   <HHfffffII> 32B  date(u16 TDX 编码) time(u16 分钟数) OHLC(f32 **已是实际价格**) amount(f32) volume(u32) rsv(u32)
    .lc5   同 .lc1

    注意 .lc1 的 OHLC 是 f32 浮点, 不需要乘系数; 而 .day 的 OHLC 是整数, 需 x0.01。
    (股票/指数/ETF/北交所 统一 x0.01, 已用真实 vipdoc 交叉验证)

用法:
    from tdxrs.local import read_daily, read_daily_mmap, read_lc

    m = read_daily(r"D:\\TDX\\vipdoc\\sh\\lday\\sh600519.day")
    m.n          # 条数
    m.raw        # (n,) structured 视图, 零拷贝
    m.raw2d      # (n, 8) uint32 视图, 零拷贝 (与磁盘字节一一对应)
    m.close      # 收盘价 (f64, x coefficient, 惰性+缓存)
    m.dates      # datetime64[D]
    m.to_dataframe()

    with read_daily_mmap(path) as m:      # 0 次显式拷贝, 需显式释放句柄
        ...
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

__all__ = [
    "Matrix", "read_daily", "read_daily_mmap", "read_daily_bytes", "read_lc",
    "read_tail", "read_range",
    "DAY_DTYPE", "LC_DTYPE", "RECORD_SIZE", "DEFAULT_COEFFICIENT",
]

RECORD_SIZE = 32
DEFAULT_COEFFICIENT = 0.01

# .day: 8 x u32 视角下的字段语义
DAY_DTYPE = np.dtype([
    ("date", "<u4"), ("open", "<u4"), ("high", "<u4"), ("low", "<u4"),
    ("close", "<u4"), ("amount", "<f4"), ("volume", "<u4"), ("reserved", "<u4"),
])

# .lc1/.lc5: <HHfffffII>
LC_DTYPE = np.dtype([
    ("date", "<u2"), ("time", "<u2"),
    ("open", "<f4"), ("high", "<f4"), ("low", "<f4"), ("close", "<f4"),
    ("amount", "<f4"), ("volume", "<u4"), ("reserved", "<u4"),
])

# 自检: dtype 必须精确等于磁盘记录长度, 否则整条零拷贝链路是错的
assert DAY_DTYPE.itemsize == RECORD_SIZE, DAY_DTYPE.itemsize
assert LC_DTYPE.itemsize == RECORD_SIZE, LC_DTYPE.itemsize

_OHLC = ("open", "high", "low", "close")


def _days_from_civil(year, month, day):
    """(y, m, d) -> 距 1970-01-01 的天数 (Howard Hinnant 算法, 向量化)。

    纯 numpy 实现, 避免为了构造 datetime64 而引入 pandas 依赖。
    """
    y = np.asarray(year, dtype=np.int64)
    m = np.asarray(month, dtype=np.int64)
    d = np.asarray(day, dtype=np.int64)
    y = np.where(m <= 2, y - 1, y)
    era = np.where(y >= 0, y, y - 399) // 400
    yoe = y - era * 400
    doy = (153 * np.where(m > 2, m - 3, m + 9) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


class Matrix:
    """32 字节定长记录的零拷贝视图 + 惰性解码列。

    Parameters
    ----------
    arr : np.ndarray
        (n,) structured 数组 (可能是只读或 memmap)。
    kind : {"day", "lc"}
    coefficient : float
        .day 价格系数 (默认 0.01); .lc 恒为 1.0 (磁盘上已是浮点价格)。
    path : Path | None
    partial_bytes : int
        文件末尾不足一条记录的残余字节数 (strict 模式应为 0)。
    """

    __slots__ = ("_arr", "_kind", "_coefficient", "_path", "_partial", "_cache")

    def __init__(self, arr, kind="day", coefficient=None, path=None, partial_bytes=0):
        self._arr = arr
        self._kind = kind
        self._coefficient = (DEFAULT_COEFFICIENT if kind == "day" else 1.0) \
            if coefficient is None else float(coefficient)
        self._path = Path(path) if path is not None else None
        self._partial = int(partial_bytes)
        self._cache: dict = {}

    # ---------- 基本属性 ----------

    @property
    def n(self) -> int:
        """记录条数。"""
        return int(self._arr.shape[0])

    def __len__(self) -> int:
        return self.n

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def coefficient(self) -> float:
        return self._coefficient

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def partial_bytes(self) -> int:
        """末尾残余字节数 (非 0 说明文件不是 32 的整数倍)。"""
        return self._partial

    @property
    def code(self) -> str | None:
        """从文件名推断带市场前缀的代码, 如 ``sh600519``。"""
        return self._path.stem if self._path is not None else None

    @property
    def raw(self):
        """(n,) structured 视图 -- 与磁盘字节一一对应, 零拷贝。"""
        return self._arr

    @property
    def raw2d(self):
        """(n, 8) uint32 视图 -- 零拷贝的二维形态, 可直接拼接成大矩阵。"""
        return self._arr.view(np.uint32).reshape(-1, 8)

    # ---------- 日期 ----------

    @property
    def dates_raw(self):
        """原始日期整数。.day 为 u32(YYYYMMDD), .lc 为 u16(TDX 编码)。"""
        return self._arr["date"]

    def _ymd(self):
        if "ymd" not in self._cache:
            raw = self._arr["date"]
            if self._kind == "day":
                # 双格式兼容: YYYYMMDD(>100000) 与 TDX 编码
                full = raw > 100000
                year = np.where(full, raw // 10000, raw // 2048 + 2004)
                month = np.where(full, (raw // 100) % 100, (raw % 2048) // 100)
                day = np.where(full, raw % 100, (raw % 2048) % 100)
            else:
                year = raw // 2048 + 2004
                month = (raw % 2048) // 100
                day = (raw % 2048) % 100
            self._cache["ymd"] = (year.astype(np.int64), month.astype(np.int64),
                                  day.astype(np.int64))
        return self._cache["ymd"]

    @property
    def year(self):
        return self._ymd()[0]

    @property
    def month(self):
        return self._ymd()[1]

    @property
    def day(self):
        return self._ymd()[2]

    @property
    def dates(self):
        """datetime64[D] 数组 (惰性 + 缓存)。"""
        if "dates" not in self._cache:
            y, m, d = self._ymd()
            days = _days_from_civil(y, m, d)
            self._cache["dates"] = (days.astype("timedelta64[D]")
                                    + np.datetime64("1970-01-01", "D"))
        return self._cache["dates"]

    @property
    def date_str(self):
        """'YYYY-MM-DD' 字符串列表 (惰性 + 缓存)。

        逐条分配字符串是这个模块里唯一「非零拷贝」的列; 只在需要可读日期时构造,
        并且缓存以支持断言器/导出场景的重复访问。
        """
        if "dstr" not in self._cache:
            y, m, d = self._ymd()
            self._cache["dstr"] = [f"{int(a):04d}-{int(b):02d}-{int(c):02d}"
                                   for a, b, c in zip(y, m, d)]
        return self._cache["dstr"]

    @property
    def hour(self):
        """.lc 的分钟线小时 (0-23); .day 无此概念。"""
        if self._kind != "lc":
            raise AttributeError(".day 记录没有 hour 字段")
        return (self._arr["time"] // 60).astype(np.int64)

    @property
    def minute(self):
        if self._kind != "lc":
            raise AttributeError(".day 记录没有 minute 字段")
        return (self._arr["time"] % 60).astype(np.int64)

    @property
    def datetimes(self):
        """.lc 的 datetime64[m] 数组。"""
        if self._kind != "lc":
            raise AttributeError(".day 记录没有 datetimes 字段")
        if "dts" not in self._cache:
            base = self.dates.astype("datetime64[m]")
            self._cache["dts"] = base + self._arr["time"].astype("timedelta64[m]")
        return self._cache["dts"]

    # ---------- 行情列 ----------

    def _price(self, field: str):
        key = f"p_{field}"
        if key not in self._cache:
            v = self._arr[field].astype(np.float64)
            if self._coefficient != 1.0:
                v = v * self._coefficient
            self._cache[key] = v
        return self._cache[key]

    @property
    def open(self):
        return self._price("open")

    @property
    def high(self):
        return self._price("high")

    @property
    def low(self):
        return self._price("low")

    @property
    def close(self):
        return self._price("close")

    @property
    def ohlc(self):
        """(n, 4) 价格矩阵。"""
        if "ohlc" not in self._cache:
            self._cache["ohlc"] = np.column_stack([self.open, self.high, self.low, self.close])
        return self._cache["ohlc"]

    @property
    def amount(self):
        if "amount" not in self._cache:
            self._cache["amount"] = self._arr["amount"].astype(np.float64)
        return self._cache["amount"]

    @property
    def volume(self):
        if "volume" not in self._cache:
            self._cache["volume"] = self._arr["volume"].astype(np.float64)
        return self._cache["volume"]

    @property
    def close_raw(self):
        """.day 收盘价的整数原值 (u32, 未乘系数) -- 需要精确比较时用它。"""
        return self._arr["close"]

    # ---------- 切片 ----------

    def __getitem__(self, item):
        """切片返回新的 Matrix (视图, 零拷贝); 单条返回结构化标量。"""
        if isinstance(item, slice):
            return Matrix(self._arr[item], self._kind, self._coefficient, self._path, 0)
        if isinstance(item, (int, np.integer)):
            return self._arr[item]
        idx = np.asarray(item)
        if idx.dtype == bool:
            idx = np.flatnonzero(idx)
        return Matrix(self._arr[idx], self._kind, self._coefficient, self._path, 0)

    def tail(self, n: int) -> "Matrix":
        """末尾 n 条 (视图, 零拷贝)。"""
        n = max(0, int(n))
        return Matrix(self._arr[-n:] if n else self._arr[:0],
                      self._kind, self._coefficient, self._path, 0)

    # ---------- 转换 ----------

    def to_dataframe(self, index: bool = False):
        """构造 pandas.DataFrame。列直接来自 numpy, 不经 list[dict]。"""
        import pandas as pd

        data = {
            "date": self.date_str if self._kind == "day" else self.datetimes,
            "open": self.open, "high": self.high, "low": self.low, "close": self.close,
            "amount": self.amount, "volume": self.volume,
        }
        if self._kind == "lc":
            data["hour"] = self.hour
            data["minute"] = self.minute
        df = pd.DataFrame(data)
        if index:
            df = df.set_index("date")
        return df

    def to_tuples(self):
        """与 DailyBarReader.parse_file_tuples 同构的 list[tuple] (兼容路径)。"""
        ds = self.date_str
        if self._kind == "day":
            return [(ds[i], float(self.open[i]), float(self.high[i]), float(self.low[i]),
                     float(self.close[i]), float(self.amount[i]), float(self.volume[i]),
                     int(self.year[i]), int(self.month[i]), int(self.day[i]))
                    for i in range(self.n)]
        dts = self.datetimes
        return [(str(dts[i])[:10] + f" {self.hour[i]:02d}:{self.minute[i]:02d}",
                 float(self.open[i]), float(self.high[i]), float(self.low[i]),
                 float(self.close[i]), float(self.amount[i]), float(self.volume[i]),
                 int(self.year[i]), int(self.month[i]), int(self.day[i]),
                 int(self.hour[i]), int(self.minute[i]))
                for i in range(self.n)]

    def to_bars(self) -> list:
        """转换为 list[dict], 键与 tdxrs.hybrid 的标准化记录一致。

        产出形态: {"date","open","high","low","close","volume","amount"}
        (仅 .day 用; 这是 HybridClient 快路径的兼容输出。)
        """
        ds = self.date_str
        o, h, l, c = self.open, self.high, self.low, self.close
        vol, amt = self.volume, self.amount
        return [{"date": ds[i], "open": float(o[i]), "high": float(h[i]),
                 "low": float(l[i]), "close": float(c[i]),
                 "volume": float(vol[i]), "amount": float(amt[i])}
                for i in range(self.n)]

    def __repr__(self) -> str:
        rng = ""
        if self.n:
            ds = self.date_str
            rng = f", {ds[0]}..{ds[-1]}"
        partial = f", partial={self._partial}B" if self._partial else ""
        return (f"Matrix(n={self.n}, kind={self._kind}"
                f"{', code=' + self.code if self.code else ''}{rng}{partial})")

    # ---------- 资源释放 (mmap) ----------

    def dispose(self) -> None:
        """释放底层 mmap 句柄 (对普通 ndarray 是 no-op)。

        命名为 dispose 而非 close: `close` 已被收盘价列占用 (与 DataFrame 列名、
        现有 API 一致), 两者不能共用。
        """
        m = getattr(self._arr, "_mmap", None)
        if m is not None:
            try:
                m.close()
            except Exception:
                pass
        self._cache.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.dispose()
        return False


# ============================================================
# 读取入口
# ============================================================

def _dtype_of(kind: str) -> np.dtype:
    return DAY_DTYPE if kind == "day" else LC_DTYPE


def _matrix_from_buffer(buf, kind: str, coefficient, path, strict: bool) -> Matrix:
    if strict and len(buf) % RECORD_SIZE != 0:
        raise ValueError(
            f"{path}: 文件大小 {len(buf)} 不是 {RECORD_SIZE} 的整数倍 "
            f"(残余 {len(buf) % RECORD_SIZE} 字节)")
    n = len(buf) // RECORD_SIZE
    arr = np.frombuffer(buf, dtype=_dtype_of(kind), count=n)
    return Matrix(arr, kind, coefficient, path, len(buf) - n * RECORD_SIZE)


def read_daily_bytes(buf, coefficient=None, path=None, strict: bool = True) -> Matrix:
    """从已有字节缓冲构造 Matrix (批量场景可复用同一 buffer)。"""
    return _matrix_from_buffer(buf, "day", coefficient, path, strict)


def read_daily(path, coefficient=None, strict: bool = True) -> Matrix:
    """读取 .day 文件 -> Matrix (1 次内核拷贝, 0 次重排, 0 次物化)。

    strict=True 时, 文件大小不是 32 整数倍会抛 ValueError (与现有 PyO3 实现一致)。
    """
    p = Path(path)
    buf = p.read_bytes()
    return _matrix_from_buffer(buf, "day", coefficient, p, strict)


def read_daily_mmap(path, coefficient=None, strict: bool = False) -> Matrix:
    """读取 .day 文件 -> Matrix (np.memmap, 0 次显式拷贝)。

    面向「数据目录可能正被写入」的场景, 因此默认 strict=False:
    末尾不足一条记录的残余字节被丢弃, 而不是报错(防读到半条记录)。
    需显式 dispose() 或使用 with 语句释放文件句柄。
    """
    p = Path(path)
    size = p.stat().st_size
    n = size // RECORD_SIZE
    if strict and size % RECORD_SIZE != 0:
        raise ValueError(f"{p}: 文件大小 {size} 不是 {RECORD_SIZE} 的整数倍")
    arr = np.memmap(p, dtype=DAY_DTYPE, mode="r", shape=(n,))
    return Matrix(arr, "day", coefficient, p, size - n * RECORD_SIZE)


def read_lc(path, strict: bool = True) -> Matrix:
    """读取 .lc1 / .lc5 分钟线文件 -> Matrix。

    OHLC 在磁盘上已是 f32 实际价格, 不乘系数。
    """
    p = Path(path)
    buf = p.read_bytes()
    return _matrix_from_buffer(buf, "lc", 1.0, p, strict)


# ============================================================
# 按需区间读取 (只碰需要的字节)
# ============================================================
#
# 动机: 筛查场景只取最近 30 根, 却把整个文件(本机均值 ~1085 条)解析一遍,
# 再把结果复制成 list[dict] 最后切片 -- 前面 1055 条从未被使用。
# 实测 6.87 ms/股 -> 0.17 ms/股 (39x, 取决于文件长度)。

def read_tail(path, n: int, coefficient=None, strict: bool = True) -> Matrix:
    """只读文件末尾 n 条记录 (seek 到尾部, 只碰 n*32 字节)。

    n 超过文件条数时返回整个文件; n <= 0 返回空 Matrix。
    """
    p = Path(path)
    size = p.stat().st_size
    if strict and size % RECORD_SIZE:
        raise ValueError(f"{p}: 文件大小 {size} 不是 {RECORD_SIZE} 的整数倍")
    total = size // RECORD_SIZE
    n = int(n)
    if n < 0:
        raise ValueError(f"n 必须 >= 0, 得到 {n}")
    n = min(n, total)
    if n == 0:
        return Matrix(np.empty(0, dtype=DAY_DTYPE), "day", coefficient, p,
                      size - total * RECORD_SIZE)
    with open(p, "rb") as f:
        f.seek((total - n) * RECORD_SIZE)
        buf = f.read(n * RECORD_SIZE)
    return _matrix_from_buffer(buf, "day", coefficient, p, False)


def _norm_date_int(raw, kind: str) -> int:
    """把任意日期编码归一化为 YYYYMMDD 整数, 用于区间比较。"""
    raw = int(raw)
    if kind == "day" and raw > 100000:
        return raw
    return (raw // 2048 + 2004) * 10000 + ((raw % 2048) // 100) * 100 + (raw % 2048) % 100


def _date_at(f, i: int, kind: str) -> int:
    f.seek(i * RECORD_SIZE)
    return int.from_bytes(f.read(4 if kind == "day" else 2), "little")


def _bound(f, total: int, key, kind: str, upper: bool) -> int:
    """二分: 返回第一个 key(归一化) >= 目标 (upper=True 时 > 目标) 的下标。"""
    lo, hi = 0, total
    while lo < hi:
        mid = (lo + hi) // 2
        v = _norm_date_int(_date_at(f, mid, kind), kind)
        if v < key or (upper and v == key):
            lo = mid + 1
        else:
            hi = mid
    return lo


def read_range(path, start=None, end=None, kind: str = "day", coefficient=None) -> Matrix:
    """按日期区间读取 [start, end] (含两端), 用于「只要某段时间」的场景。

    文件内日期严格升序, 因此用二分定位首尾偏移, 只读该区间。
    start / end 接受 'YYYY-MM-DD' 字符串或 YYYYMMDD 整数; None 表示取到端点。
    本机 .day 均值 ~1085 条, 二分仅需 ~11 次 seek。
    """
    p = Path(path)
    size = p.stat().st_size
    total = size // RECORD_SIZE
    dt = _dtype_of(kind)
    coef = coefficient
    if kind == "day" and coef is None:
        coef = DEFAULT_COEFFICIENT

    def _key(v):
        if v is None:
            return None
        if isinstance(v, str):
            y, m, d = v.replace("/", "-")[:10].split("-")
            return int(y) * 10000 + int(m) * 100 + int(d)
        return _norm_date_int(v, kind)

    ks, ke = _key(start), _key(end)
    if ks is not None and ke is not None and ks > ke:
        raise ValueError(f"start({start}) 晚于 end({end})")

    if total == 0:
        return Matrix(np.empty(0, dtype=dt), kind, coef, p, size)

    with open(p, "rb") as f:
        lo = 0 if ks is None else _bound(f, total, ks, kind, upper=False)
        hi = total if ke is None else _bound(f, total, ke, kind, upper=True)
        if hi < lo:
            hi = lo
        f.seek(lo * RECORD_SIZE)
        buf = f.read((hi - lo) * RECORD_SIZE)
    return _matrix_from_buffer(buf, kind, coef, p, False)
