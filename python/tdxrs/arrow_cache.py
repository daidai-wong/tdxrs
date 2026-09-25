# -*- coding: utf-8 -*-
"""Arrow 列存缓存 —— 把「一次性转置落盘」产品化。

为什么需要它
------------
vipdoc 是 32 字节行主序定长记录, 每次解析都要付一次「行主序 -> 列式」的转置
(实测 16.2 ms/百万行)。而调用方的常见形态是**反复读同一批数据**:
回测、选股、因子计算、多策略共享一份全市场快照。于是::

    重扫 vipdoc   scan_daily          869 ms   (997.9 万行 / 304.5 MiB)
    建缓存        + Arrow + Feather  1381 ms   一次性
    重开          open_table        99.6 ms   比重扫快 8.7x
    重开 (DuckDB 直读 Parquet)       48.0 ms   快 18x

回本点 **n ≈ 1.8** —— 同一份数据要被打开 2 次以上就该建缓存。

三条设计决定
------------
1. **单一 manifest.json 是唯一事实来源**: schema 版本 / 格式 / 源目录 / 列集合 /
   分片清单 / **每个源文件的指纹** ``(size, mtime_ns, rows)`` 全在里面。
   元数据一次原子替换, 不存在「半更新」的中间态。
2. **分片用内容寻址**: 文件名形如 ``shard-0003.a1b2c3d4.feather``, 后缀是该分片
   「成员指纹集合」的 blake2b 前缀。重写一个分片 = 写一个新名字的文件, 再原子
   换 manifest, 旧文件在换完之后才回收。
   → 进程在任何时刻被杀, 磁盘上要么是完整旧状态、要么是完整新状态; 也不会出现
   「manifest 指向写了一半的分片」。
3. **分片归属 = stem 的稳定哈希**(不是文件列表下标)。若按排序下标分片, 新增一只
   股票会让后面所有文件的归属平移, 全部变脏分片; 用哈希则新文件落进既有分片。
   ``n_shards`` 在建缓存时定下并记进 manifest, 之后**永不改变**(否则映射会重排)。

诚实的限制
----------
* **日线数据每天几乎每个文件都会变**(每只股票多一根 K 线) → 所有分片都脏,
  ``update()`` 退化为全量重建。返回值的 ``is_full_rebuild`` 会如实报告这一点。
  想避免每日全量, 需要「按文件追加」的存储形态, 那不是 Arrow/Parquet 的模型。
* ``open_table()`` 的**行序是分片序**, 不等于 ``scan_daily`` 的文件排序。
  ``to_panel(sort=True)`` 可还原成完全等价(需一次 gather 拷贝)。
* ``memory_map=True`` 读 Feather **不是零拷贝**(pyarrow 仍会把数据拷出来),
  详见 :mod:`tdxrs.boundary`。
* 价格列按原始 ``uint32``(×100)存, 不是 f64 —— 精确且省一半内存。
* 分片文件是**内容寻址**的, 所以同名文件天然可复用; ``_write_shards`` 会先查
  磁盘上是否已有该 tag 的文件, 有就跳过扫描 —— 这让 ``build()`` 幂等且廉价。

用法
----
    from tdxrs.arrow_cache import ArrowCache, open_cache

    c = ArrowCache("D:/cache/sh_lday")
    c.build(r"D:/TDX/vipdoc/sh/lday")               # 首次 (全市场 ~1.4 s)
    c.update()                                      # 之后每天: 只重写脏分片

    tb = c.open_table(columns=["close", "volume"])   # 列裁剪
    df = c.to_pandas()                               # split_blocks -> 零拷贝
    panel = c.to_panel()                             # 还原成 DailyPanel (可 grid())
    con, name = c.duckdb()                           # DuckDB 直读 Parquet

    c.info(); c.status(); c.verify()                 # 元数据 / 新鲜度 / 校验

pyarrow 是**可选依赖**, 本模块自身不 import 它 (只在需要时导入)。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from tdxrs import local as _local

__all__ = [
    "ArrowCache", "CacheInfo", "UpdateReport", "VerifyReport",
    "SCHEMA_VERSION", "MANIFEST_NAME", "build_cache", "open_cache",
]

SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
DEFAULT_SHARD_SIZE = 512
SHARD_PREFIX = "shard"

# 一个目录只能有一种列存格式
_EXT = {"feather": ".feather", "parquet": ".parquet"}

# 8 个 32 位槽位的语义 (.day 与 .lc 在槽 0 之后的布局完全一致 —— 这正是
# 两种 kind 可以共用一套槽位索引的原因; .lc 的槽 0 是 date(u16)+time(u16))
_DAY_SLOTS = ("date", "open", "high", "low", "close", "amount", "volume",
              "reserved")
_LC_SLOTS = ("date", "time", "open", "high", "low", "close", "amount",
             "volume", "reserved")
_PRICE = ("open", "high", "low", "close")
_AMOUNT_SLOT = 5


def _import_pyarrow():
    try:
        import pyarrow as pa
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "Arrow 缓存需要 pyarrow: pip install pyarrow\n"
            "(tdxrs 核心读取路径不依赖 pyarrow)"
        ) from e
    return pa


def _slots_of(kind: str) -> tuple:
    return _LC_SLOTS if kind == "lc" else _DAY_SLOTS


def _source_repr(source):
    """构建来源的可回放表示。

    目录/单文件 -> 原样保留 (``update()``/``status()`` 能重新扫描发现新增文件);
    **文件列表 -> None** —— 列表没法回放 (增量时必须知道"现在有哪些文件"),
    记成 ``str(list)`` 更是错的 (会变成一坨巨长字符串被当成路径)。
    此时 ``update()`` 会明确要求显式传 ``source=``。
    """
    if isinstance(source, (str, os.PathLike)):
        return str(source)
    return None


def _exts_of(kind: str) -> tuple:
    return (".day",) if kind == "day" else (".lc1", ".lc5")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _fingerprint(p: Path):
    """[size, mtime_ns, rows] —— rows 由文件长度推出, 不需要读内容。"""
    st = p.stat()
    return [int(st.st_size), int(st.st_mtime_ns),
            int(st.st_size // _local.RECORD_SIZE)]


def _fsync_file(path) -> None:
    """把文件内容刷到磁盘。

    Windows 上 ``FlushFileBuffers`` 要求句柄**可写** —— 用 ``open(path, "rb")``
    拿到的只读句柄调 ``os.fsync`` 会抛 ``OSError: [Errno 9] Bad file descriptor``,
    所以这里显式用 ``O_RDWR`` 打开。
    """
    fd = os.open(str(path), os.O_RDWR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _shard_of(stem: str, n_shards: int) -> int:
    """stem -> 分片号。稳定哈希: 与文件列表顺序、文件总数都无关。"""
    h = hashlib.blake2b(stem.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "little") % max(1, int(n_shards))


def _shard_tag(members: dict) -> str:
    """分片成员指纹集合 -> 8 位十六进制内容标记 (分片文件名的版本部分)。"""
    h = hashlib.blake2b(digest_size=8)
    for stem in sorted(members):
        size, mtime, rows = members[stem]
        h.update(f"{stem}\0{size}\0{mtime}\0{rows}\0".encode("utf-8"))
    return h.hexdigest()[:8]


def _fps_equal(a: dict, b: dict) -> bool:
    if a.keys() != b.keys():
        return False
    return all(tuple(a[k]) == tuple(b[k]) for k in a)


# ============================================================
# 返回结构
# ============================================================
@dataclass
class CacheInfo:
    """缓存元数据快照。"""
    root: str
    kind: str = "day"
    format: str = "feather"
    compression: str = ""
    source: str = ""
    n_shards: int = 0
    shard_size: int = 0
    columns: list = field(default_factory=list)
    with_code: bool = True
    n_files: int = 0
    n_rows: int = 0
    source_bytes: int = 0
    cache_bytes: int = 0
    built_at: str = ""
    updated_at: str = ""
    build_seconds: float = 0.0

    @property
    def ratio(self) -> float:
        """缓存体积 / 源体积。"""
        return round(self.cache_bytes / self.source_bytes, 4) \
            if self.source_bytes else 0.0

    @property
    def rows_per_file(self) -> float:
        return round(self.n_rows / self.n_files, 1) if self.n_files else 0.0

    def __repr__(self) -> str:
        return (f"CacheInfo({self.kind}/{self.format}, {self.n_files} 文件 / "
                f"{self.n_rows:,} 行, {len(self.columns)} 列, "
                f"{self.n_shards} 分片, {self.cache_bytes / 2**20:.1f} MiB"
                + (f" ({self.ratio:.2f}x 源)" if self.ratio else "") + ")")


@dataclass
class UpdateReport:
    """一次 ``update()`` 做了什么 —— 每个数字都可核对。"""
    n_files: int = 0
    n_rows: int = 0
    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    modified: list = field(default_factory=list)
    n_shards: int = 0
    dirty_shards: list = field(default_factory=list)
    dropped_shards: list = field(default_factory=list)
    rows_written: int = 0
    files_reread: int = 0
    shards_written: int = 0
    shards_reused: int = 0
    seconds: float = 0.0

    @property
    def n_changed(self) -> int:
        return len(self.added) + len(self.removed) + len(self.modified)

    @property
    def is_full_rebuild(self) -> bool:
        """所有分片都脏 -> 本次等同全量重建 (日线每日更新的常态)。"""
        return bool(self.n_shards) and len(self.dirty_shards) >= self.n_shards

    @property
    def saved_ratio(self) -> float:
        """省掉的源文件重读比例 (1.0 = 一个都没重读)。"""
        if not self.n_files:
            return 0.0
        return 1.0 - self.files_reread / self.n_files

    def __repr__(self) -> str:
        return (f"UpdateReport(+{len(self.added)} -{len(self.removed)} "
                f"~{len(self.modified)}; 脏分片 {len(self.dirty_shards)}"
                f"/{self.n_shards}, 重读 {self.files_reread}/{self.n_files} "
                f"文件, 省 {self.saved_ratio:.0%}, {self.seconds:.3f}s"
                + (", 等同全量重建" if self.is_full_rebuild else "") + ")")


@dataclass
class VerifyReport:
    """``verify()`` 结果: 缓存是否仍与源目录一致、分片是否都在。"""
    ok: bool = True
    added_files: list = field(default_factory=list)     # 源里新增, 缓存里没有
    missing_files: list = field(default_factory=list)   # 缓存里有, 源里已消失
    modified_files: list = field(default_factory=list)  # 指纹变了
    missing_shards: list = field(default_factory=list)  # 分片文件丢失
    size_mismatch: list = field(default_factory=list)   # 分片大小与 manifest 不符
    source_comparable: bool = True   # False = manifest 未记录可回放的来源
    n_checked: int = 0

    @property
    def stale(self) -> bool:
        return bool(self.missing_files or self.added_files
                    or self.modified_files)

    def __repr__(self) -> str:
        if not self.source_comparable:
            return (f"VerifyReport({'OK' if self.ok else 'BAD'}, "
                    f"分片丢 {len(self.missing_shards)}, 来源不可比")
        if self.ok and not self.stale:
            return f"VerifyReport(ok, {self.n_checked} 文件一致)"
        return (f"VerifyReport({'OK' if self.ok else 'BAD'}, "
                f"新增 {len(self.added_files)} / 消失 {len(self.missing_files)}"
                f" / 变更 {len(self.modified_files)}"
                f" / 分片丢 {len(self.missing_shards)})")


# ============================================================
# 主体
# ============================================================
class ArrowCache:
    """分片式 Arrow 列存缓存 (单目录: manifest.json + 若干内容寻址分片)。

    一个实例绑定一个目录 + 一种 kind + 一种列集合。**参数以 manifest 为准**:
    :meth:`open` 不接收参数, 避免「同一个目录被两种列集合交替打开」这种静默错误。
    """

    def __init__(self, root, kind: str = "day", fmt: str = "feather",
                 shard_size: int = DEFAULT_SHARD_SIZE, columns=None,
                 with_code: bool = True, date_as: str = "raw",
                 price_scale: bool = False, compression: str | None = None):
        if kind not in ("day", "lc"):
            raise ValueError(f"kind 只支持 day / lc, 得到 {kind!r}")
        if fmt not in _EXT:
            raise ValueError(f"format 只支持 {list(_EXT)}, 得到 {fmt!r}")
        if date_as not in ("raw", "date32"):
            raise ValueError(f"date_as 只支持 raw / date32, 得到 {date_as!r}")
        self.root = Path(root)
        self._kind = kind
        self._fmt = fmt
        self._shard_size = max(1, int(shard_size))
        self._columns = self._norm_columns(columns, kind)
        self._with_code = bool(with_code)
        self._date_as = date_as
        self._price_scale = bool(price_scale)
        self._compression = compression
        self._manifest: dict | None = None

    # ---------- 列集合 ----------
    @staticmethod
    def _norm_columns(columns, kind: str, allow_empty: bool = False) -> list:
        """用户给的列集合 -> 规范化后的**逻辑槽位**列名 (`code`/`date32` 另算)。

        ``allow_empty=True`` 供读取路径使用 (只请求 ``['code']`` 是合法的);
        构建路径不允许空数据列集合。
        """
        slots = _slots_of(kind)
        if columns is None:
            return list(slots)              # 默认全槽 -> 可无损还原行主序矩阵
        cols = [columns] if isinstance(columns, str) else list(columns)
        auto = {"code", "date32"}
        explicit = [c for c in cols if c not in auto]
        bad = [c for c in explicit if c not in slots]
        if bad:
            raise ValueError(
                f"未知列 {bad}; 可选 {list(slots)} (+\"code\"/\"date32\", "
                f"由 with_code / date_as 控制)")
        if not explicit and not allow_empty:
            raise ValueError(f"columns 至少要含一个数据列, 得到 {cols!r}")
        return explicit

    def _stored_columns(self) -> list:
        """缓存里**实际存在**的列 (按表内顺序)。"""
        cols = list(self._columns)
        if self._date_as == "date32" and "date32" not in cols:
            cols.append("date32")
        if self._with_code and "code" not in cols:
            cols.append("code")
        return cols

    # ---------- 生命周期 ----------
    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def format(self) -> str:
        return self._fmt

    def exists(self) -> bool:
        return self.manifest_path.is_file()

    @classmethod
    def open(cls, root) -> "ArrowCache":
        """从 manifest 读回完整参数。"""
        root = Path(root)
        mp = root / MANIFEST_NAME
        if not mp.is_file():
            raise FileNotFoundError(
                f"{root} 下没有 {MANIFEST_NAME}; "
                f"先 ArrowCache({str(root)!r}).build(source)")
        m = json.loads(mp.read_text(encoding="utf-8"))
        if int(m.get("schema", 0)) != SCHEMA_VERSION:
            raise ValueError(
                f"缓存 schema={m.get('schema')} 与本模块 {SCHEMA_VERSION} 不符; "
                f"请删目录重建或 ArrowCache.open(...).rebuild()")
        c = cls(root, kind=m["kind"], fmt=m["format"],
                shard_size=m["shard_size"], columns=m["_slot_columns"],
                with_code=m["with_code"], date_as=m["date_as"],
                price_scale=m["price_scale"], compression=m.get("compression"))
        c._manifest = m
        return c

    def _require_manifest(self) -> dict:
        if self._manifest is None:
            if not self.exists():
                raise FileNotFoundError(
                    f"{self.root} 尚未构建; 先调 build(source)")
            self._manifest = json.loads(
                self.manifest_path.read_text(encoding="utf-8"))
        return self._manifest

    def _discard_manifest(self) -> None:
        self._manifest = None

    # ---------- 计划 / 分片归属 ----------
    def _plan(self, files: list, n_shards: int):
        """-> (plan, paths); plan = {shard_id: {stem: [size, mtime, rows]}}"""
        plan: dict = {}
        paths: dict = {}
        for p in files:
            stem = p.stem
            if stem in paths:
                raise ValueError(
                    f"文件名冲突: {stem!r} 出现两次 ({paths[stem]} 与 {p}); "
                    f"缓存以文件 stem 为唯一键 (与 DailyPanel.codes 一致), "
                    f"请拆到不同缓存目录")
            paths[stem] = p
            plan.setdefault(_shard_of(stem, n_shards), {})[stem] = _fingerprint(p)
        return plan, paths

    @staticmethod
    def _n_shards_for(n_files: int, shard_size: int) -> int:
        return max(1, -(-int(n_files) // max(1, int(shard_size))))

    # ---------- 构建 ----------
    def build(self, source, *, force: bool = False, workers: int = 0) -> CacheInfo:
        """首次构建 (已有缓存时报错, 除非 ``force=True``)。

        全市场 9233 文件 / 1021 万行实测 **1.4 s** 级 (扫描 + 转置 + 落盘)。
        """
        if self.exists() and not force:
            raise FileExistsError(
                f"{self.root} 已有缓存; 增量请用 update(), 覆盖请传 force=True")
        return self._write(source, workers=workers)

    def rebuild(self, source=None, *, workers: int = 0) -> CacheInfo:
        """丢弃全部分片重建 (改了 kind / 列集合 / 格式时用)。"""
        src = source if source is not None else \
            self._require_manifest().get("source")
        if src is None:
            raise ValueError(
                "manifest 里没记录可回放的来源 (构建时传的是文件列表), "
                "请显式传 source=<目录或文件列表>")
        self._discard_manifest()
        return self._write(src, workers=workers)

    def _write(self, source, *, workers: int) -> CacheInfo:
        files = _local._resolve_files(source, _exts_of(self._kind))
        n_shards = self._n_shards_for(len(files), self._shard_size)
        plan, paths = self._plan(files, n_shards)
        t0 = time.perf_counter()
        shards, _rep = self._write_shards(
            plan, paths, plan, workers=workers)
        shards.sort(key=lambda s: s["id"])
        self._commit(old={}, shards=shards, source=_source_repr(source),
                     files=files, n_shards=n_shards,
                     seconds=time.perf_counter() - t0)
        return self.info()

    def update(self, source=None, *, workers: int = 0,
               force: bool = False) -> UpdateReport:
        """增量更新: 只重扫 / 只重写指纹变化的分片。

        日线数据的常态是「每个文件都多了一根 K 线」—— 此时所有分片都脏,
        ``is_full_rebuild`` 为 True, 如实告知本次等同全量重建。
        """
        m = self._require_manifest()
        src = source if source is not None else m.get("source")
        if src is None:
            raise ValueError(
                "manifest 里没记录可回放的来源目录 (构建时传的是**文件列表**), "
                "增量更新请显式传 source=<目录或文件列表>")
        files = _local._resolve_files(src, _exts_of(self._kind))
        n_shards = int(m["n_shards"])
        plan, paths = self._plan(files, n_shards)

        cur = {int(s["id"]): {r[0]: [r[1], r[2], r[3]] for r in s["files"]}
               for s in m["shards"]}
        # 磁盘上分片文件丢失 (误删/半途中断) 也算脏 —— update() 顺手修好,
        # 不必让调用方先跑 verify() 再 rebuild()。
        broken = {int(s["id"]) for s in m["shards"]
                  if not (self.root / s["file"]).is_file()}

        rep = UpdateReport(n_files=len(files), n_shards=n_shards)
        dirty: dict = {}
        for sid in sorted(set(plan) | set(cur)):
            want, have = plan.get(sid, {}), cur.get(sid, {})
            if sid not in broken and not force and _fps_equal(want, have):
                continue
            for stem in want:
                if stem not in have:
                    rep.added.append(stem)
                elif tuple(have[stem]) != tuple(want[stem]):
                    rep.modified.append(stem)
            rep.removed += [s for s in have if s not in want]
            if not want:
                rep.dropped_shards.append(sid)
                continue
            dirty[sid] = want
        rep.added.sort()
        rep.modified.sort()
        rep.removed.sort()

        t0 = time.perf_counter()
        new_shards, wrep = self._write_shards(plan, paths, dirty,
                                             workers=workers)
        rep.dirty_shards = sorted(dirty)
        rep.rows_written = wrep["rows"]
        rep.files_reread = wrep["files_reread"]
        rep.shards_written = wrep["written"]
        rep.shards_reused = wrep["reused"]

        keep = {int(s["id"]): s for s in m["shards"]}
        shards = [s for sid, s in keep.items()
                  if sid not in dirty and sid not in rep.dropped_shards]
        shards += new_shards
        shards.sort(key=lambda s: s["id"])
        self._commit(old=m, shards=shards, source=src, files=files,
                     n_shards=n_shards, seconds=time.perf_counter() - t0)
        rep.n_rows = int(self._manifest["n_rows"])
        rep.seconds = time.perf_counter() - t0
        return rep

    # ---------- 分片落盘 ----------
    def _write_shards(self, plan: dict, paths: dict, dirty: dict, *,
                      workers: int):
        """把 dirty 的分片写到磁盘 (内容寻址 + 原子替换)。

        内容寻址带来一个免费优化: tag 相同 => 内容一定相同 => 磁盘上若已有该
        文件就**连扫描都省掉**。这让 ``build()`` 幂等且廉价, 也让「分片文件被
        误删后重建」不需要重读源数据。
        """
        out: list = []
        rows = files_reread = written = reused = 0
        for sid in sorted(dirty):
            members = dirty[sid]
            tag = _shard_tag(members)
            name = f"{SHARD_PREFIX}-{sid:04d}.{tag}{_EXT[self._fmt]}"
            final = self.root / name
            if final.is_file():
                reused += 1
                n_rows = sum(members[s][2] for s in members)
            else:
                member_paths = [paths[s] for s in sorted(members)]
                panel = _local._scan(member_paths, self._kind, 0, workers, None)
                self._atomic_write(self._panel_to_table(panel), final)
                n_rows = int(panel.n_rows)
                rows += n_rows
                files_reread += len(members)
                written += 1
            out.append({
                "id": int(sid),
                "file": name,
                "tag": tag,
                "n_rows": int(n_rows),
                "n_files": len(members),
                "bytes": int(final.stat().st_size),
                "files": [[stem, *members[stem]] for stem in sorted(members)],
            })
        return out, {"rows": rows, "files_reread": files_reread,
                     "written": written, "reused": reused}

    def _atomic_write(self, table, final: Path) -> None:
        """临时文件 -> fsync -> os.replace。读者永远不会看到半个文件。"""
        from tdxrs import boundary
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(f"{final.name}.tmp-{os.getpid()}")
        try:
            if self._fmt == "feather":
                boundary.write_feather(
                    table, tmp, compression=self._compression or "lz4")
            else:
                boundary.write_parquet(
                    table, tmp, compression=self._compression or "zstd")
            _fsync_file(tmp)                 # 先落盘再换名 (Windows 上尤其必要)
            os.replace(tmp, final)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:  # pragma: no cover
                    pass

    def _panel_to_table(self, panel):
        """DailyPanel -> Arrow Table: 整块只做**一次**转置, 8 列共享连续 buffer。

        实测逐文件转置再拼接比整块转置慢 2.7x。``.lc`` 的槽 0 是
        ``date(u16)+time(u16)`` 打包, 在这里按位拆开 (唯一一处额外小拷贝)。
        """
        pa = _import_pyarrow()
        t = np.ascontiguousarray(panel.raw2d.T)     # (8, n) u4, 唯一一次拷贝
        tf = t.view(np.float32)
        cols = {}
        for nm in self._columns:
            if nm == "date" and self._kind == "lc":
                cols["date"] = pa.array((t[0] & 0xFFFF).astype(np.uint32))
            elif nm == "time":
                cols["time"] = pa.array((t[0] >> np.uint32(16)).astype(np.uint32))
            elif nm == "date":
                cols["date"] = pa.array(t[0])
            elif nm == "amount":
                cols["amount"] = pa.array(tf[_AMOUNT_SLOT])
            else:
                i = _DAY_SLOTS.index(nm)            # .lc 槽 0 之后与 .day 同构
                col = tf[i] if (self._kind == "lc" and nm in _PRICE) else t[i]
                if self._price_scale and nm in _PRICE and self._kind == "day":
                    col = col.astype(np.float64) * panel.coefficient
                cols[nm] = pa.array(col)
        if self._date_as == "date32":
            cols["date32"] = pa.array(panel.merged().dates)
        if self._with_code:
            cols["code"] = pa.DictionaryArray.from_arrays(
                pa.array(panel.code_indices()),
                pa.array([str(c) for c in panel.codes]))
        return pa.table(cols)

    # ---------- manifest 提交 + 垃圾回收 ----------
    def _commit(self, old: dict, shards: list, source, files: list, *,
                n_shards: int, seconds: float) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        source_bytes = 0
        for p in files:
            try:
                source_bytes += p.stat().st_size
            except OSError:  # pragma: no cover
                pass
        m = {
            "schema": SCHEMA_VERSION,
            "kind": self._kind,
            "format": self._fmt,
            "compression": self._compression,
            "source": None if source is None else str(source),
            "shard_size": self._shard_size,
            "n_shards": int(n_shards),
            "_slot_columns": list(self._columns),
            "columns": self._stored_columns(),
            "with_code": self._with_code,
            "date_as": self._date_as,
            "price_scale": self._price_scale,
            "coefficient": 1.0 if self._kind == "lc" else _local.DEFAULT_COEFFICIENT,
            "n_files": len(files),
            "n_rows": int(sum(s["n_rows"] for s in shards)),
            "source_bytes": int(source_bytes),
            "cache_bytes": int(sum(s["bytes"] for s in shards)),
            "built_at": old.get("built_at") or _now_iso(),
            "updated_at": _now_iso(),
            "build_seconds": round(float(seconds), 4),
            "shards": shards,
        }
        tmp = self.manifest_path.with_name(
            f"{MANIFEST_NAME}.tmp-{os.getpid()}")
        tmp.write_text(json.dumps(m, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        _fsync_file(tmp)
        os.replace(tmp, self.manifest_path)      # <- 唯一的原子切换点
        self._manifest = m
        self._gc()

    def _gc(self) -> list:
        """删除 manifest 不再引用的分片 (旧版本 + 孤儿); 跳过在写的临时文件。"""
        keep = {s["file"] for s in (self._manifest or {}).get("shards", [])}
        removed = []
        for p in self.root.glob(f"{SHARD_PREFIX}-*"):
            if p.name in keep or ".tmp-" in p.name:
                continue
            try:
                p.unlink()
                removed.append(p.name)
            except OSError:  # pragma: no cover
                pass
        return removed

    # ---------- 读取 ----------
    def shard_paths(self) -> list:
        m = self._require_manifest()
        return [self.root / s["file"] for s in m["shards"]]

    def locate(self, code: str) -> str:
        """某个证券落在哪个分片文件里 (方便人工排查)。"""
        m = self._require_manifest()
        stem = str(code)
        sid = _shard_of(stem, int(m["n_shards"]))
        for s in m["shards"]:
            if int(s["id"]) == sid:
                return s["file"]
        return f"(分片 {sid} 当前为空)"

    def open_table(self, columns=None, *, shards=None):
        """读回 Arrow Table; ``columns`` 列裁剪, ``shards`` 只取指定分片。

        ``columns`` 只能请求缓存里**已存**的列 —— 想加列要 ``rebuild(columns=...)``。
        """
        from tdxrs import boundary
        m = self._require_manifest()
        stored = list(m["columns"])
        want = stored if columns is None else \
            (self._norm_columns(columns, self._kind, allow_empty=True)
             + [c for c in ("code", "date32")
                if isinstance(columns, (list, tuple, set)) and c in columns])
        bad = [c for c in want if c not in stored]
        if bad:
            raise ValueError(
                f"缓存里没有列 {bad}; 已存 {stored} (想加列请 rebuild(columns=...))")
        if not want:
            raise ValueError("至少要请求一列")
        entries = m["shards"] if shards is None else \
            [s for s in m["shards"] if int(s["id"]) in set(shards)]
        tables = []
        for s in entries:
            p = self.root / s["file"]
            if not p.is_file():
                raise FileNotFoundError(
                    f"分片 {p.name} 丢失; 跑 verify() 确认后 rebuild()")
            tables.append(boundary.open_feather(p, columns=want)
                          if self._fmt == "feather"
                          else boundary.open_parquet(p, columns=want))
        if not tables:
            return _empty_table(want)
        return tables[0] if len(tables) == 1 else \
            _import_pyarrow().concat_tables(tables, promote_options="default")

    def to_pandas(self, columns=None, **kw):
        """Table -> DataFrame (默认 ``split_blocks=True``, 避免 pandas 再拷一遍)。"""
        from tdxrs import boundary
        return boundary.to_pandas(self.open_table(columns), **kw)

    def _fill_slots(self, buf, tb, a: int, b: int, slots, slot_of) -> None:
        """把一列式表按行区间写入 (8, n) 缓冲的 ``[a, b)``。

        按行连续写 (比散布写 (n,8) 快 ~25%); 整块转置留到全部填充完之后做一次。
        ``.lc`` 的槽 0 是 ``date(u16)+time(u16)`` 打包, 这里按位拼回。
        """
        time_col = tb["time"].to_numpy(zero_copy_only=False) \
            if self._kind == "lc" else None
        for nm in slots:
            if nm == "time":
                continue
            col = tb[nm].to_numpy(zero_copy_only=False)
            if nm == "date" and self._kind == "lc":
                buf[0, a:b] = (col.astype(np.uint32)
                               | (time_col.astype(np.uint32) << np.uint32(16)))
            elif nm == "amount" or (self._kind == "lc" and nm in _PRICE):
                buf[slot_of[nm], a:b] = np.asarray(col, np.float32).view(np.uint32)
            else:
                buf[slot_of[nm], a:b] = col.astype(np.uint32, copy=False)

    def to_panel(self, columns=None, *, sort: bool = False):
        """还原成 :class:`~tdxrs.local.DailyPanel` (可继续用 grid/categorical)。

        需要把列式 buffer **逆向重排**回行主序 —— 成本 ≈ 一次转置;
        实测让缓存重开依然比重扫 vipdoc 快数倍。

        ``sort=False`` (默认) 行序是**分片序**, 与 ``scan_daily`` 的文件排序不同
        (``codes``/``offsets`` 自洽, ``grid()`` 只是行序不同)。
        要完全等价于 ``scan_daily`` 传 ``sort=True`` (多一次 gather 拷贝)。
        """
        m = self._require_manifest()
        slots = _slots_of(self._kind)
        missing = [c for c in slots if c not in m["_slot_columns"]]
        if missing:
            raise ValueError(
                f"缓存缺列 {missing}, 无法无损还原行主序矩阵; "
                f"用 rebuild(columns=None) 重建完整槽位")
        tb_cols = list(slots)
        total = int(m["n_rows"])
        # 逐分片填充: 每个分片单独打开、写完就放开。这样**不需要把整张列式表同时
        # 驻留**(全市场 1021 万行整表 ~820 MB), 峰值只剩输出缓冲的一份。
        # pyarrow 的内存池不会把释放的缓冲还给 OS, 所以「先读全表再 del」是无效的
        # —— 唯一有效的办法是根本不要同时持有整表。
        buf = np.empty((8, total), dtype=np.uint32)
        slot_of = {nm: i for i, nm in enumerate(_DAY_SLOTS)}
        at = 0
        for s in m["shards"]:
            cnt = int(s["n_rows"])
            if cnt:
                self._fill_slots(buf, self.open_table(tb_cols, shards=[s["id"]]),
                                 at, at + cnt, slots, slot_of)
            at += cnt
        arr2d = np.ascontiguousarray(buf.T)          # (n, 8) u4, 唯一一次拷贝
        del buf
        dt = _local.DAY_DTYPE if self._kind == "day" else _local.LC_DTYPE
        arr = arr2d.reshape(-1).view(dt)

        order = [(r[0], r[3]) for s in m["shards"] for r in s["files"]]
        counts = np.array([c for _s, c in order], dtype=np.int64)
        if sort:
            starts = np.zeros(len(counts), dtype=np.int64)
            if len(counts) > 1:
                np.cumsum(counts[:-1], out=starts[1:])
            perm = np.argsort([s for s, _c in order], kind="stable")
            idx = np.concatenate([np.arange(starts[i], starts[i] + counts[i])
                                  for i in perm]) if len(perm) else \
                np.empty(0, dtype=np.int64)
            arr = arr[idx]                            # 一次 gather (拷贝)
            order = [order[i] for i in perm]
            counts = counts[perm]
        offsets = np.zeros(len(counts) + 1, dtype=np.int64)
        np.cumsum(counts, out=offsets[1:])
        src = m.get("source") or "."
        files = [Path(src) / f"{stem}{_exts_of(self._kind)[0]}"
                 for stem, _c in order]
        return _local.DailyPanel(arr, offsets, files, self._kind,
                                 float(m.get("coefficient") or 0.01))

    def duckdb(self, columns=None, *, con=None, name: str = "daily",
               source: str = "parquet"):
        """注册进 DuckDB。

        ``source='parquet'`` 用 ``read_parquet(glob)`` 直读 (推荐 —— 列裁剪 +
        谓词下推都在 DuckDB 侧发生, 实测全市场 scan + 聚合 48 ms);
        ``source='arrow'`` 先把 Table 读进内存再 register (整表载入, 但零拷贝)。
        """
        try:
            import duckdb
        except ImportError as e:  # pragma: no cover
            raise ImportError("需要 duckdb: pip install duckdb") from e
        con = con or duckdb.connect()
        if source == "parquet" and self._fmt == "parquet":
            glob = str(self.root / f"{SHARD_PREFIX}-*{_EXT['parquet']}")
            cols = "*" if columns is None else ", ".join(
                list(self._norm_columns(columns, self._kind, allow_empty=True))
                + [c for c in ("code", "date32")
                   if isinstance(columns, (list, tuple, set)) and c in columns])
            con.execute(f"CREATE OR REPLACE VIEW {name} AS "
                        f"SELECT {cols} FROM read_parquet('{glob}')")
        else:
            con.register(name, self.open_table(columns))
        return con, name

    # ---------- 元数据 ----------
    def info(self) -> CacheInfo:
        m = self._require_manifest()
        return CacheInfo(
            root=str(self.root), kind=m["kind"], format=m["format"],
            compression=m.get("compression") or "", source=m.get("source") or "",
            n_shards=int(m["n_shards"]), shard_size=int(m["shard_size"]),
            columns=list(m["columns"]), with_code=bool(m["with_code"]),
            n_files=int(m["n_files"]), n_rows=int(m["n_rows"]),
            source_bytes=int(m["source_bytes"]),
            cache_bytes=int(m["cache_bytes"]),
            built_at=m.get("built_at", ""), updated_at=m.get("updated_at", ""),
            build_seconds=float(m.get("build_seconds") or 0.0))

    def _current_fps(self, n_shards: int):
        m = self._require_manifest()
        return {int(s["id"]): {r[0]: [r[1], r[2], r[3]] for r in s["files"]}
                for s in m["shards"]}

    def _src_of(self, source):
        """解析出可用于比对的来源; 返回 (src, 是否可知)。"""
        m = self._require_manifest()
        src = source if source is not None else m.get("source")
        return src, src is not None

    def status(self, source=None) -> dict:
        """一眼看出缓存是否需要更新 (只 stat 源文件, 不读分片内容)。

        ``fresh=None`` 表示 manifest 里没记录可回放的来源 (构建时传的是**文件
        列表**), 此时无法判断新鲜度 —— 需要显式传 ``source=``。
        """
        m = self._require_manifest()
        src, known = self._src_of(source)
        if not known:
            return {"root": str(self.root), "fresh": None, "dirty_shards": None,
                    "n_shards": int(m["n_shards"]),
                    "n_files_cached": int(m["n_files"]),
                    "n_files_source": None, "n_rows": int(m["n_rows"]),
                    "updated_at": m.get("updated_at"),
                    "cache_bytes": int(m["cache_bytes"]),
                    "note": "manifest 未记录可回放的来源 (构建时传的是文件列表); "
                            "传 source= 才能比较"}
        files = _local._resolve_files(src, _exts_of(self._kind))
        plan, _paths = self._plan(files, int(m["n_shards"]))
        cur = self._current_fps(int(m["n_shards"]))
        dirty = sum(1 for sid in set(plan) | set(cur)
                    if not _fps_equal(plan.get(sid, {}), cur.get(sid, {})))
        return {
            "root": str(self.root),
            "fresh": dirty == 0,
            "dirty_shards": dirty,
            "n_shards": int(m["n_shards"]),
            "n_files_cached": int(m["n_files"]),
            "n_files_source": len(files),
            "n_rows": int(m["n_rows"]),
            "updated_at": m.get("updated_at"),
            "cache_bytes": int(m["cache_bytes"]),
        }

    def stale_files(self, source=None) -> dict:
        """具体哪些文件变了: added / removed / modified。

        来源不可知 (构建时传的是文件列表) 时返回三个空列表 + ``note``, 而不是
        把全部文件都报成 modified。
        """
        m = self._require_manifest()
        src, known = self._src_of(source)
        if not known:
            return {"added": [], "removed": [], "modified": [],
                    "note": "来源不可回放, 无法比较 (传 source=)"}
        files = _local._resolve_files(src, _exts_of(self._kind))
        want = {p.stem: tuple(_fingerprint(p)) for p in files}
        have = {r[0]: (r[1], r[2], r[3])
                for s in m["shards"] for r in s["files"]}
        return {
            "added": sorted(set(want) - set(have)),
            "removed": sorted(set(have) - set(want)),
            "modified": sorted(k for k in set(want) & set(have)
                               if want[k] != have[k]),
        }

    def verify(self, source=None, *, check_shard_size: bool = True) -> VerifyReport:
        """校验: 源目录是否变了 + 分片文件是否都在、大小是否与 manifest 相符。"""
        m = self._require_manifest()
        d = self.stale_files(source)
        rep = VerifyReport(added_files=d["added"], missing_files=d["removed"],
                           modified_files=d["modified"], n_checked=int(m["n_files"]))
        rep.source_comparable = "note" not in d
        for s in m["shards"]:
            p = self.root / s["file"]
            if not p.is_file():
                rep.missing_shards.append(s["file"])
            elif check_shard_size and p.stat().st_size != int(s["bytes"]):
                rep.size_mismatch.append(s["file"])
        rep.ok = not (rep.missing_shards or rep.size_mismatch)
        return rep

    def clear(self) -> None:
        """删除整个缓存目录的内容。"""
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self._manifest = None

    def __repr__(self) -> str:
        if self.exists():
            try:
                return f"ArrowCache({self.info()!r})"
            except Exception:  # pragma: no cover
                pass
        return f"ArrowCache({str(self.root)!r}, 未构建)"


def _empty_table(columns):
    pa = _import_pyarrow()
    return pa.table({c: pa.array([], type=pa.null()) for c in columns})


# ============================================================
# 便捷入口
# ============================================================
def build_cache(root, source, *, kind: str = "day", fmt: str = "feather",
                shard_size: int = DEFAULT_SHARD_SIZE, columns=None,
                with_code: bool = True, date_as: str = "raw",
                price_scale: bool = False, workers: int = 0,
                force: bool = False) -> CacheInfo:
    """建缓存并返回元数据。"""
    c = ArrowCache(root, kind=kind, fmt=fmt, shard_size=shard_size,
                   columns=columns, with_code=with_code, date_as=date_as,
                   price_scale=price_scale)
    return c.build(source, force=force, workers=workers)


def open_cache(root, source=None, *, auto_update: bool = False,
               workers: int = 0) -> ArrowCache:
    """打开缓存; ``auto_update=True`` 时探测到脏分片就自动增量更新。

    典型每日流程::

        c = open_cache("D:/cache/sh_lday", source="D:/TDX/vipdoc/sh/lday",
                       auto_update=True)
        df = c.to_pandas(columns=["code", "date", "close"])
    """
    c = ArrowCache.open(root)
    if auto_update:
        st = c.status(source)
        if st["fresh"] is False:          # None = 来源不可比, 不擅自更新
            c.update(source, workers=workers)
    return c
