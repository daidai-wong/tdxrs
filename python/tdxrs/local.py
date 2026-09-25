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
    "Matrix", "DailyPanel",
    "read_daily", "read_daily_mmap", "read_daily_bytes", "read_lc",
    "read_tail", "read_range", "scan_daily", "scan_lc",
    "DAY_DTYPE", "LC_DTYPE", "RECORD_SIZE", "DEFAULT_COEFFICIENT",
    "DEFAULT_WORKERS",
]

RECORD_SIZE = 32
DEFAULT_COEFFICIENT = 0.01
# 本机实测 8 线程是拐点 (再往上每文件 open/close 的固定开销占比上升),
# 且并行读同一块磁盘在冷缓存下会互相竞争, 故默认封顶为 8。
DEFAULT_WORKERS = 8

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

# 8 个 32 位槽位的字段名 (.day 与 .lc 布局一致)
_COL_INDEX = {"date": 0, "open": 1, "high": 2, "low": 3,
              "close": 4, "amount": 5, "volume": 6, "reserved": 7}


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

    def to_arrow(self, **kwargs):
        """-> pyarrow.Table (边界互操作; 惰性依赖 pyarrow, 见 tdxrs.boundary)。

        只做边界, 不进热路径: 记录是行主序, 取列是跨步视图, Arrow 必须拷贝一次。
        ``columns=`` 按需建列是这里最有效的开关 (要 1 列时比整体转置便宜 ~9x)。
        """
        from tdxrs.boundary import matrix_to_arrow
        return matrix_to_arrow(self, **kwargs)

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


# ============================================================
# 批量并行扫描 (一次调用读完一个目录 / 文件列表)
# ============================================================
#
# 动机: 全市场 9233 个 .day 文件 = 9233 次调用 + 9233 次 open/close
# + 9233 个独立结果对象再拼装。scan_daily 把这三项都收敛成一次:
#
# 两遍法:
#   ① 逐文件 stat 求行数 -> cumsum -> offsets -> 预分配单个 (total,) 缓冲
#   ② 并行填充各自的行区间 (区间互不重叠, 天然无锁, 无需 GIL 之外同步)
#
# 相比「逐文件 frombuffer 再 np.concatenate」:
#   * 少一次全量拷贝 (concatenate 的产物是独立分配);
#   * 峰值内存从「所有分片同时存活」降到「单缓冲 + 单个在途分片」。
#
# tail=N 时只读每个文件末尾 N 条, 与 read_tail 等价 —— 筛查场景直接
# 一次吃完整个目录, 只碰 N*32*K 字节。

def _resolve_files(target, exts: tuple) -> list[Path]:
    """target: 目录 / 单个文件 / 路径序列 -> 排序后的 Path 列表。

    目录优先按 exts 直接匹配; 若空则递归 (兼容直接传 vipdoc 根目录)。
    """
    if isinstance(target, (str, os.PathLike)):
        p = Path(target)
        if p.is_dir():
            files: list[Path] = []
            for e in exts:
                files += list(p.glob(f"*{e}"))
            if not files:
                for e in exts:
                    files += list(p.rglob(f"*{e}"))
            return sorted(files)
        return [p]
    return [Path(x) for x in target]


class DailyPanel:
    """批量扫描结果: 单个预分配大矩阵 + 分段索引。

    内存布局与「逐个 read_daily 再拼」完全一致 (行主序 struct-of-records),
    但只有一份连续分配, 且每个文件的行区间可由 ``offsets`` O(1) 定位。

    Attributes
    ----------
    raw2d : (N, 8) uint32
        零拷贝二维视图, 与磁盘字节一一对应 (N = 总行数)。
    offsets : (K+1,) int64
        第 i 个文件占 ``[offsets[i], offsets[i+1])`` 行 (K = 文件数)。
    codes : list[str]
        带市场前缀的代码 (取自文件名 stem, 如 ``sh600519``)。
    """

    __slots__ = ("_arr", "_offsets", "_files", "_kind", "_coefficient", "_cache")

    def __init__(self, arr, offsets, files, kind, coefficient=None):
        self._arr = arr
        self._offsets = offsets
        self._files = files
        self._kind = kind
        self._coefficient = (DEFAULT_COEFFICIENT if kind == "day" else 1.0) \
            if coefficient is None else float(coefficient)
        self._cache: dict = {}

    # ---------- 基本属性 ----------

    @property
    def n_rows(self) -> int:
        """总行数。"""
        return int(self._arr.shape[0])

    @property
    def n_files(self) -> int:
        return len(self._files)

    def __len__(self) -> int:
        return self.n_files

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def coefficient(self) -> float:
        """价格系数 (.day 默认 0.01; .lc 恒为 1.0)。"""
        return self._coefficient

    @property
    def raw(self):
        """(N,) structured 视图 -- 零拷贝。"""
        return self._arr

    @property
    def raw2d(self):
        """(N, 8) uint32 视图 -- 零拷贝, 可直接当列式矩阵用。"""
        return self._arr.view(np.uint32).reshape(-1, 8)

    @property
    def offsets(self):
        """(K+1,) int64 分段边界。"""
        return self._offsets

    @property
    def counts(self):
        """(K,) int64 每个文件的行数。"""
        return np.diff(self._offsets)

    @property
    def files(self) -> list:
        return list(self._files)

    @property
    def paths(self) -> list:
        return [str(f) for f in self._files]

    @property
    def codes(self) -> list:
        """带市场前缀的代码列表 (文件名 stem)。"""
        return [f.stem for f in self._files]

    # ---------- 分段索引 ----------

    def _code_index(self) -> dict:
        if "cindex" not in self._cache:
            idx = {}
            for i, f in enumerate(self._files):
                idx[f.stem] = i                      # sh600519
                idx[f.stem[2:]] = i                  # 600519 (裸代码, 后者仅在无歧义时可信)
            self._cache["cindex"] = idx
        return self._cache["cindex"]

    def index_of(self, code: str) -> int:
        """代码 -> 文件序号。接受 ``sh600519`` 或 ``600519``; 找不到抛 KeyError。"""
        idx = self._code_index()
        c = str(code)
        if c in idx:
            return idx[c]
        # 裸代码可能同时存在于沪深 (000001), 此时取第一个匹配
        for k, v in idx.items():
            if k.endswith(c):
                return v
        raise KeyError(f"面板中没有 {code} (共 {len(self._files)} 个文件)")

    def matrix_at(self, i: int) -> Matrix:
        """第 i 个文件的行区间 -> Matrix (视图, 零拷贝)。"""
        i = int(i)
        if i < 0:
            i += len(self._files)
        if not 0 <= i < len(self._files):
            raise IndexError(f"文件序号 {i} 越界 (共 {len(self._files)} 个)")
        a, b = int(self._offsets[i]), int(self._offsets[i + 1])
        return Matrix(self._arr[a:b], self._kind, self._coefficient, self._files[i], 0)

    def matrix(self, code: str) -> Matrix:
        """按代码取该文件的行区间 -> Matrix (视图)。"""
        return self.matrix_at(self.index_of(code))

    # ---------- code 列 (Categorical 承载) ----------

    def row_code_index(self):
        """(N,) int64: 每一行属于第几个文件。"""
        if "rci" not in self._cache:
            self._cache["rci"] = np.repeat(
                np.arange(len(self._files), dtype=np.int64), self.counts)
        return self._cache["rci"]

    def code_indices(self, dtype=np.int32):
        """(N,) 整数索引 -> code 的字典下标 (惰性 + 按 dtype 缓存)。

        Arrow 的 dictionary 编码与 pandas 的 Categorical 都吃整数下标,
        且都要求 ≤ int32; 缓存下来才能保证「零拷贝进 Arrow」的地址断言成立
        (每次现转 astype 都会得到新数组, 地址必然不同)。
        """
        dt = np.dtype(dtype)
        key = f"ci_{dt.str}"
        if key not in self._cache:
            self._cache[key] = self.row_code_index().astype(dt)
        return self._cache[key]

    def categorical(self):
        """code 列 -> pd.Categorical (dictionary 编码)。

        326 万行实测 6.3 MB, 而 object 字符串列 24.9 MB。
        """
        import pandas as pd
        if "cat" not in self._cache:
            self._cache["cat"] = pd.Categorical.from_codes(
                self.code_indices(), categories=self.codes)
        return self._cache["cat"]

    # ---------- 转换 ----------

    def merged(self) -> Matrix:
        """整个面板当作一个 Matrix (零拷贝; 日期不再按文件重置, 调用方自负语义)。"""
        return Matrix(self._arr, self._kind, self._coefficient, None, 0)

    def as_cube(self):
        """所有分段等长时返回 (K, n, 8) uint32 立方体视图 (零拷贝)。

        分段严格等长时(如对一批老股 ``tail=N`` 扫描)可直接零拷贝重排::

            c = scan_daily(old_files, tail=30).as_cube()
            closes = c[:, :, 4] * 0.01              # (K, 30) 收盘价
            amounts = c.view(np.float32)[:, :, 5]   # amount 是 f32, 需换视角

        现实里新股/停牌股历史不足, 分段往往不等长 —— 此时抛 ValueError,
        请改用 :meth:`grid` (右对齐 + 填充, 不受长度差异影响)。
        """
        c = self.counts
        if c.size == 0:
            return self.raw2d.reshape(0, 0, 8)
        n = int(c[0])
        if not np.all(c == n):
            bad = int(np.flatnonzero(c != n)[0])
            raise ValueError(
                f"分段不等长 (第 {bad} 段 {int(c[bad])} 条 != {n} 条); "
                f"新股/停牌会导致此情况, 请改用 grid()")
        return self.raw2d.reshape(len(self._files), n, 8)

    def grid(self, column: str = "close", width: int | None = None,
             fill=float("nan"), coefficient=None):
        """右对齐等宽网格 (K, width) —— 筛查场景的主力输出形态。

        每行的最后一条记录对齐到最后一列, 历史不足 width 条的行用 ``fill``
        左填充。这样既避免 DataFrame 与逐条 Python 对象, 又不受
        「新股历史不足」影响::

            g = scan_daily(dir, tail=30).grid("close")     # (K, 30) f64
            up = g[:, -1] > g[:, 0] * 1.05                 # 近 30 日涨超 5%

        Parameters
        ----------
        column : str
            open / high / low / close / amount / volume / reserved。
            price 列(.day 的 OHLC)乘系数; .lc 的 OHLC 磁盘上已是 f32 实际价格。
        width : int | None
            网格宽度; None 取最长分段的长度。
        fill : float
            历史不足时的填充值 (默认 NaN, 便于直接参与比较/聚合)。
        """
        if column not in _COL_INDEX:
            raise ValueError(f"未知列 {column!r}; 可选 {sorted(_COL_INDEX)}")
        if column == "date":
            raise ValueError("日期列请用 date_grid(); grid() 只处理数值列")
        import numpy as _np

        field = self._arr[column]                 # (N,) 原生类型 (u4 / f4)
        counts = self.counts
        k = len(self._files)
        width = int(width) if width else int(counts.max() if counts.size else 0)
        out = _np.full((k, width), fill, dtype=_np.float64)
        coef = self._coefficient if coefficient is None else float(coefficient)
        scale = coef if column in _OHLC else 1.0
        for i in range(k):
            m = int(counts[i])
            if m == 0:
                continue
            w = min(m, width)
            end = int(self._offsets[i + 1])
            out[i, width - w:] = field[end - w:end].astype(_np.float64) * scale
        return out

    def close_grid(self, width: int | None = None, fill=float("nan")):
        """(K, width) 收盘价网格 (f64, 已乘系数)。"""
        return self.grid("close", width, fill)

    def volume_grid(self, width: int | None = None, fill=float("nan")):
        """(K, width) 成交量网格 (f64)。"""
        return self.grid("volume", width, fill)

    def date_grid(self, width: int | None = None):
        """(K, width) 日期网格 (datetime64[D], 不足处为 NaT)。"""
        counts = self.counts
        k = len(self._files)
        width = int(width) if width else int(counts.max() if counts.size else 0)
        out = np.full((k, width), np.datetime64("NaT", "D"), dtype="datetime64[D]")
        for i in range(k):
            m = int(counts[i])
            if m == 0:
                continue
            w = min(m, width)
            a = int(self._offsets[i + 1]) - w
            seg = Matrix(self._arr[a:a + w], self._kind, self._coefficient,
                         self._files[i], 0)
            out[i, width - w:] = seg.dates
        return out

    def to_dataframe(self, with_code: bool = True, index: bool = False,
                     date_as: str = "datetime64"):
        """整体 DataFrame; ``with_code=True`` 时首列为 Categorical 的 ``code``。

        Parameters
        ----------
        date_as : {"datetime64", "str", "raw"}
            日期列形态。批量场景下这一列是「唯一的非向量化热点」:
            212 万行实测 datetime64 远快于逐条格式化; ``raw`` 直接给出
            u32 的 YYYYMMDD 整数 (零转换)。``str`` 与 :meth:`Matrix.to_dataframe`
            的传统形态一致 (逐条分配, 仅在与旧输出对齐时使用)。
        """
        import pandas as pd

        m = self.merged()
        # 注意: 不要用 df.insert(0, ...) —— pandas 对合并后的 block 调 insert
        # 会触发一次整表复制 (全市场 1021 万行时多出数百 MB)。把 code 放进
        # 构造字典的第一位即可, 且列顺序天然正确。
        data: dict = {}
        if with_code:
            data["code"] = self.categorical()
        if date_as == "datetime64":
            data["date"] = m.dates if self._kind == "day" else m.datetimes
        elif date_as == "raw":
            data["date"] = np.asarray(m.dates_raw)
        elif date_as == "str":
            data["date"] = m.date_str if self._kind == "day" else m.datetimes
        else:
            raise ValueError(f"date_as 只支持 datetime64 / str / raw, 得到 {date_as!r}")
        data.update(open=m.open, high=m.high, low=m.low, close=m.close,
                    amount=m.amount, volume=m.volume)
        if self._kind == "lc":
            data["hour"] = m.hour
            data["minute"] = m.minute
        df = pd.DataFrame(data)
        if index:
            df = df.set_index("code" if with_code else "date")
        return df

    def to_arrow(self, **kwargs):
        """-> pyarrow.Table (整个面板一张列存表; ``code`` 为 dictionary 编码)。"""
        from tdxrs.boundary import panel_to_arrow
        return panel_to_arrow(self, **kwargs)

    def __repr__(self) -> str:
        rng = ""
        if self.n_rows and len(self._files):
            m0 = self.matrix_at(0)
            rng = f", {m0.date_str[0]}.."
            last = self.matrix_at(-1)
            if last.n:
                rng += last.date_str[-1]
        return (f"DailyPanel(files={self.n_files}, rows={self.n_rows}"
                f", kind={self._kind}{rng})")


# ============================================================
# 批量扫描入口
# ============================================================

def _scan(target, kind: str, tail: int, workers: int, coefficient) -> DailyPanel:
    exts = (".day",) if kind == "day" else (".lc1", ".lc5")
    files = _resolve_files(target, exts)
    dt = _dtype_of(kind)
    tail = max(0, int(tail))

    # ---- 第一遍: 求行数 -> offsets -> 预分配 ----
    counts = np.zeros(len(files), dtype=np.int64)
    for i, f in enumerate(files):
        try:
            c = f.stat().st_size // RECORD_SIZE
        except OSError:
            c = 0
        counts[i] = min(c, tail) if tail else c
    offsets = np.zeros(len(files) + 1, dtype=np.int64)
    np.cumsum(counts, out=offsets[1:])
    big = np.empty(int(offsets[-1]), dtype=dt)

    # ---- 第二遍: 并行填充各自区间 (互不重叠, 无需锁) ----
    def fill(i: int) -> None:
        c = int(counts[i])
        if c == 0:
            return
        a = int(offsets[i])
        with open(files[i], "rb") as fh:
            if tail:
                end = (fh.seek(0, os.SEEK_END) // RECORD_SIZE) * RECORD_SIZE
                fh.seek(end - c * RECORD_SIZE)
            buf = fh.read(c * RECORD_SIZE)
        n = len(buf) // RECORD_SIZE
        if n:
            big[a:a + n] = np.frombuffer(buf, dtype=dt, count=n)

    n_files = len(files)
    if n_files:
        if workers == 0:
            workers = min(DEFAULT_WORKERS, os.cpu_count() or 1, n_files)
        if workers > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(workers) as ex:
                list(ex.map(fill, range(n_files)))
        else:
            for i in range(n_files):
                fill(i)

    return DailyPanel(big, offsets, files, kind, coefficient)


def scan_daily(target, tail: int = 0, workers: int = 0, coefficient=None) -> DailyPanel:
    """批量读取 .day -> DailyPanel (单缓冲 + 分段索引)。

    Parameters
    ----------
    target : str | Path | Sequence
        目录 (如 ``D:/TDX/vipdoc/sh/lday``)、单个文件、或文件路径序列。
    tail : int
        只读每个文件末尾 ``tail`` 条; 0 表示整文件 (默认)。
    workers : int
        并行线程数; 0 表示自动 (min(8, CPU 数, 文件数)); 1 表示串行。
    coefficient : float | None
        价格系数, 默认 0.01。

    用法::

        panel = scan_daily(r"D:/TDX/vipdoc/sh/lday")
        panel.n_rows, panel.n_files
        panel.matrix("sh600519").close              # 单文件视图 (零拷贝)
        df = panel.to_dataframe()                   # 含 Categorical code 列

        panel30 = scan_daily(r"D:/TDX/vipdoc/sh/lday", tail=30)   # 只读尾部 30 条
    """
    return _scan(target, "day", tail, workers, coefficient)


def scan_lc(target, tail: int = 0, workers: int = 0) -> DailyPanel:
    """批量读取 .lc1 / .lc5 分钟线 -> DailyPanel。

    与 :func:`scan_daily` 同构; ``target`` 为 ``minline`` 目录时自动同时在
    .lc1 与 .lc5 中匹配。OHLC 在磁盘上已是 f32 实际价格, 不乘系数。
    """
    return _scan(target, "lc", tail, workers, None)
