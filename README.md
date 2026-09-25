# tdxrs — 通达信行情数据解析库 (Rust + Python)

[English](README_en.md) | 中文

[![Rust](https://img.shields.io/badge/Rust-1.83%2B-orange)](https://www.rust-lang.org/)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-brightgreen)](LICENSE)
[![PyPI version](https://img.shields.io/pypi/v/tdxrs?color=blue&label=PyPI)](https://pypi.org/project/tdxrs/)
[![PyPI downloads](https://img.shields.io/pypi/dm/tdxrs?color=blue&label=Downloads)](https://pypi.org/project/tdxrs/)
[![GitHub stars](https://img.shields.io/github/stars/jiangtaovan/tdxrs?color=yellow&label=Stars)](https://github.com/jiangtaovan/tdxrs)
[![GitHub forks](https://img.shields.io/github/forks/jiangtaovan/tdxrs?label=Forks)](https://github.com/jiangtaovan/tdxrs)
[![GitHub last commit](https://img.shields.io/github/last-commit/jiangtaovan/tdxrs?label=Updated)](https://github.com/jiangtaovan/tdxrs/commits)

**tdxrs** 是通达信 (TDX) 行情数据解析库的 Rust 高性能实现。通过 PyO3/maturin 提供 Python 调用接口，保持与 Python [tdxpy](https://github.com/rainx/pytdx) 的 API 兼容，本地解析性能提升 **9-11 倍**。

> 说明：上述 9-11× 为与 tdxpy 的对比。在同机、同口径的独立复测中该倍数为 **1.10×**；本库量级更大的收益来自
> **零拷贝本地读取路径**（全市场 997.9 万条贴到 IO 地板，筛查场景 14.2×）。详见下方 [性能](#性能) 与 [已知问题](#已知问题与限制诚实声明)。

```python
from tdxrs import TdxHqClient
from tdxrs.constants import MARKET_SH, KLINE_DAILY, FQ_QFQ

client = TdxHqClient()
client.connect_to_any()

# 贵州茅台日K → DataFrame
df = client.get_security_bars_dataframe(KLINE_DAILY, MARKET_SH, "600519", 0, 500)
df["ma20"] = df["close"].rolling(20).mean()

# 批量实时行情 (上限 60 只/次)
quotes = client.get_security_quotes([
    (MARKET_SH, "600519"), (0, "000858"), (0, "300750")
])
```

---

## 性能

> **口径说明（重要）**：本节的本地解析数字全部可用 `tests/bench_local.py` 复现，并同时给出
> **「耗时 ÷ 同轮纯 IO 地板」的归一化比值**。原因是本机 IO 地板实测在 **0.889 s ~ 5.4 s** 之间漂移（**5.7×**）——
> 同一个「零拷贝对现状 4.80×」与「1.76×」都是真数字，差别只在落在哪个 IO 窗口。
> **只报绝对耗时没有可比性**，这是本节所有表格都带 `/IO` 列的原因。
>
> 官方宣称的「本地解析提升 9-11 倍」是**与 tdxpy 对比**的结果。在同机、同口径、同语料的
> 独立复测中，该倍数为 **1.10×**（详见 `PERF_COMPARISON_REPORT.html`）；本库真正量级更大的收益来自
> **零拷贝读取路径**（下节），与 tdxpy 无关。

### 本地文件解析 — 全市场（9300 个 `.day` / 997.9 万条 / 304.5 MiB）

测试环境：i5-13400F (10C/16T) · 31.8 GB · Windows 11 Pro 28000 · rustc 1.96.0 (`opt-level=3` + `lto=true`) ·
CPython 3.13 · numpy 2.5 · pandas 3.0。复现：`python tests/bench_local.py --rounds 3 --out bench_local.json`

| 路径 | 耗时 | ÷同轮IO地板 | 峰值内存 |
|------|-----:|-----------:|--------:|
| 纯 IO 地板（逐文件 `read_bytes` 后即弃） | 1.100 s | 1.00× | 30 MB |
| `to_dataframe_file`（原有 Rust API，1 线程） | 5.274 s | 4.79× | 84 MB |
| **`local.read_daily` 零拷贝（1 线程）** | **1.286 s** | **1.17×** | 42 MB |
| **`local.scan_daily`（自动并行 + 两遍法预分配）** | **0.833 s** | **0.76×** | 373 MB |

> 零拷贝路径的绝对耗时已经**贴到 IO 地板**（1.17×），并行后甚至**低于串行地板**（0.76×）——
> 剩下唯一的杠杆是并行 IO 与输出形态，**不在解析本身**。

### 筛查场景（`HybridClient.get_daily_bars(count=30)`，300 只）

| 路径 | 耗时 | ÷旧行为 |
|------|-----:|--------:|
| 改造前：全量解析整个文件（均值 1085 条）再切片 | 0.369 s | 1.00× |
| 改造后：只读尾部 `count×32` 字节 | **0.026 s** | **14.2×**（冷 IO 口径 34.9×） |

### 边界互操作（Arrow / Parquet / DuckDB）

**Arrow 只在边界，永远不进热路径** —— 32 B 行主序记录取列是跨步视图，Arrow 必然付一次拷贝
（实测 16.17 ms/百万行）。这次拷贝换来的是「一次转置、之后不再重扫 vipdoc」：

| 环节（997.9 万行） | 耗时 | 说明 |
|------|-----:|------|
| 重扫 vipdoc（`scan_daily`） | 0.869 s | 每次都要付 |
| 建列存缓存（Arrow + Feather lz4） | 1.381 s | 一次性，188 MiB（0.62×） |
| **重开缓存** | **0.0996 s** | 8.7× 于重扫 |
| **DuckDB 直读 Parquet** | **0.048 s** | 18× 于重扫，峰值仅 73.5 MB |

回本点 **n ≈ 1.8**：缓存用到第 2 次即回本。完整实测见 `ARROW_BOUNDARY_REPORT.html`。

### 网络 API (连接池模式)

| 操作 | tdxrs | tdxpy | 加速比 |
|------|------:|------:|:-----:|
| K 线 100 条 | 73ms | 110ms | **1.5×** |
| 行情 3 只 | 75ms | 95ms | **1.3×** |
| 日K 800 条 | 290ms | — | — |

### 并发性能 (60 线程)

| 方案 | 5 线程 | 60 线程 | 扩展比 |
|------|------:|------:|:-----:|
| 裸连接 (Direct) | 381ms | 344ms | **0.9×** (零退化) |
| 连接池 (Pool) | 337ms | 4110ms | 12.2× |
| 异步 (Async) | 345ms | 3880ms | 11.2× |

> 详见 [性能基准](docs/public/BENCHMARKS.md)。

### 已知问题与限制（诚实声明）

| 项 | 状态 | 说明 |
|---|---|---|
| 编译参数调优 | **已评估，收益不可测** | `codegen-units=1` / `target-cpu=native` / `panic=abort` 五组配置在真实数据微基准上差异 **≤2%**，而**同一二进制连跑三次的噪声达 +59%（IO）/ ±12%（计算核）** —— 收益无法与噪声区分。`lto=true` 已开启，`codegen-units` 基本冗余；解码核是内存带宽瓶颈的标量循环，不吃指令集。因此**未启用**（`panic="abort"` 另有语义问题：会把 pyo3 本可转成 Python 异常的 panic 变成进程 abort） |
| `memory_map=True` 非零拷贝 | 已实测确认 | `feather.read_table(memory_map=True)` 两次读取 buffer 地址不同，即 pyarrow 仍拷出数据。真实收益是**免重扫 + 列裁剪** |
| `Table.to_pandas()` 默认拷贝 | 已确认 | pandas BlockManager 会把同 dtype 多列合并成二维 block。需 `split_blocks=True` 或 `types_mapper=pd.ArrowDtype` |
| 交易日历 | **未实现** | 请求限流按**时钟**判断交易日，**节假日会误判为 trading** |
| `async_client` 实时分时 | 走回退路径 | 同步客户端已接真实 `0x051d`；异步客户端仍走历史接口回退（功能正常，非实时） |
| `quote` 服务器时间 | 乱码 | `servertime` 字段解析未对齐（P2，不影响行情数据） |
| 基准可复现性 | 受本机影响 | IO 地板漂移 5.7×，跨机器对比必须用归一化比值；`tests/bench_gate.py` 因此只比比值 |
| Arrow 边界 | **可选依赖** | `pyarrow` / `polars` / `duckdb` 均不在 `dependencies` 里；核心读取路径不依赖它们 |


---

## 功能

### 网络行情 (13 类数据)

| 数据 | 覆盖 |
|------|------|
| **K 线** | 个股 + 指数，12 种周期 (1分钟 ~ 年线) |
| **实时行情** | 五档盘口，含成交额/总量 |
| **分时数据** | 当日 + 历史 (按日期查询) |
| **逐笔成交** | 当日 + 历史 (按日期查询，自动翻页) |
| **证券信息** | 全市场列表 + 数量 (带缓存) |
| **财务数据** | 实时 34 项 + 45 个英文命名财务指标 |
| **除权除息** | 分红/送股/配股/缩股历史 |
| **板块数据** | 行业/概念/地域分类 |

### 基金数据 (ETF/LOF/REITs/分级基金)

`TdxHqFundClient` 提供基金专用 API，接口格式与股票一致：

```python
from tdxrs import TdxHqFundClient
from tdxrs.constants import MARKET_SH, MARKET_SZ, KLINE_DAILY

client = TdxHqFundClient()
client.connect_to_any()

# 基金实时行情 (上限 60 只/次)
quotes = client.get_fund_quotes([
    (MARKET_SH, "510300"),   # 沪深300ETF
    (MARKET_SZ, "159915"),   # 创业板ETF
])

# 基金 K 线 (接口与股票相同)
bars = client.get_fund_bars(KLINE_DAILY, MARKET_SH, "510300", 0, 100)

# 基金列表
funds = client.get_fund_list(MARKET_SH)  # 返回 code, name, fund_type
```

| 类型 | 代码前缀 | 示例 |
|------|---------|------|
| ETF | 510/512/513/515/516, 159 | 510300 沪深300ETF |
| LOF | 501/502, 160/161 | 160105 南方积配 |
| REITs | 508 | 508000 普洛斯 |
| 分级基金 | 162/163/164 | 162006 银华锐进 |
| 债券基金 | 511 | 511010 国债ETF |
| 场外基金 | 519 | 519003 海富通收益 |

> 详细说明见 [基金模块文档](docs/public/FUND.md)。

### 客户端侧复权计算

TDX 服务端返回未复权原始数据。tdxrs 在客户端自行计算前复权/后复权：
- 中国 A 股标准除权除息公式
- 支持分红+送股+配股联动
- 自动补全早期除权事件 (context_bars 机制)
- `fq=0` 路径零额外开销

### 四种客户端方案

| 客户端 | 策略 | 场景 |
|-------|------|------|
| `TdxHqClient` | 连接池(5) + 心跳 + 重试 + 缓存 | 主力，顺序请求 |
| `TdxHqFundClient` | 共享连接池 + 基金代码验证 | 基金数据 |
| `TdxDirectClient` | 每请求独立 TCP | 高并发 (60线程零退化) |
| `AsyncTdxHqClient` | tokio 异步 + 心跳 | 异步生态集成 |

### 请求限流

内置交易时段自适应限流，保护服务器：

| 时段 | 默认限流 | 说明 |
|------|:--------:|------|
| 盘中 (9:30-15:00) | 15 req/s | 交易活跃期 |
| 盘前/盘后 | 30 req/s | 过渡时段 |
| 休市 | 60 req/s | 非交易日 |

```python
client = TdxHqClient()
client.connect_to_any()
client.auto_detect_phase()  # 自动检测当前时段
# 或手动设置
client.set_phase("trading")  # trading / prepost / closed
```

> 每连接独立限流，4 连接池实际吞吐 ×4。批量行情单次上限 60 只，超出自动截断。

### 本地文件解析

| 格式 | Reader | 输出 |
|------|--------|------|
| `.day` 日线 | `DailyBarReader` | dict / tuple / DataFrame |
| `.lc5` `.lc1` 分钟线 | `MinBarReader` `LcMinBarReader` | 同上 |
| `.dat` 板块 | `BlockReader` | flat / group 两种模式 |
| `gpcw*.dat` 财务 | `FinancialReader` | f32 字段数组 |

### 批量下载 (`tdxrs.downloader`)

多服务器分发 + 自动翻页 + 增量更新 + 断点续传：

```python
from tdxrs.downloader import Downloader

# 日线下载 (默认 .day 格式，原始数据 fq=0)
dl = Downloader(data_dir="./data")
dl.run(markets=["sh", "sz"], categories=["daily"])
dl.update()  # 增量更新 (仅 fq=0 支持)

# 按日下载分时/逐笔数据 (需指定股票代码)
dl.download_minute(dates=["2026-06-25"], codes=["600519", "000858"])
dl.download_ticks(dates=["2026-06-25"], codes=["600519"])
```

### CLI 命令行

无需编写代码，直接在终端查询行情：

```bash
tdxrs quote 600519,000858          # 实时行情
tdxrs bars 600519 --count 30 --fq 1  # K线 (前复权)
tdxrs trades 600519 --count 100      # 逐笔成交
tdxrs download --market sh --category daily  # 批量下载
tdxrs servers                        # 测试服务器
```

> 完整文档见 [CLI 使用指南](docs/public/CLI.md)。

---

## 安装

```bash
pip install tdxrs
```

或从源码构建：

```bash
git clone https://github.com/jiangtaovan/tdxrs && cd tdxrs
pip install maturin
maturin develop --release
```

Windows `x86_64-pc-windows-gnu` 需额外安装 [MSYS2 dlltool](docs/INSTALL.md)。详见 [安装说明](docs/INSTALL.md)。

---

## 快速示例

### K 线 — 完整复权演示

```python
from tdxrs import TdxHqClient
from tdxrs.constants import MARKET_SH, KLINE_DAILY, KLINE_WEEKLY, FQ_QFQ, FQ_HFQ, FQ_NONE

client = TdxHqClient()
client.connect_to_any()

# 前复权 (默认)
bars = client.get_security_bars(KLINE_DAILY, MARKET_SH, "600519", 0, 100)

# 未复权原始数据
raw = client.get_security_bars(KLINE_DAILY, MARKET_SH, "600519", 0, 100, fq=FQ_NONE)

# 后复权
hfq = client.get_security_bars(KLINE_DAILY, MARKET_SH, "600519", 0, 100, fq=FQ_HFQ)

# 周K + 自动分页 (3000条)
all_bars = client.get_security_bars_all(KLINE_WEEKLY, MARKET_SH, "600519", count=3000)

# Tuple 高性能模式 (快 40-60%)
tuples = client.get_security_bars_tuples(KLINE_DAILY, MARKET_SH, "600519", 0, 500)
# → (open, close, high, low, vol, amount, year, month, day, hour, minute, datetime)

client.disconnect()
```

### 多股票批量财务

```python
# 实时财务 (TDX 原始值, 不自动转换单位)
info = client.get_finance_info(market=1, code="600519")
# 经验规则: 股本类 ≈万元, 资产类 ≈万元, 每股指标 ≈元
print(f"净资产: {info['jingzichan']:.0f}")   # e.g. 270894048 → 2709亿元
print(f"每股净资产: {info['meigujingzichan']:.2f}")  # 216.32元

# 多股票对比 DataFrame
df = client.get_finance_info_dataframe([
    (MARKET_SH, "600519"), (MARKET_SZ, "000858"), (MARKET_SZ, "300750")
])
print(df[["code", "jingzichan", "jinglirun", "meigujingzichan"]])
```

### 本地文件解析

```python
from tdxrs import DailyBarReader

reader = DailyBarReader(coefficient=0.01)
df = reader.to_dataframe(open("600519.day", "rb").read())
# df.columns: date, open, high, low, close, amount, volume, year, month, day
```

---

## 工程亮点

```
语言:    Rust 2021 edition, 0 行 unsafe
测试:    139 个单元/集成测试
依赖:    6 个核心 crate (pyo3, flate2, tokio, serde, thiserror, encoding_rs)
文档:    12 篇维护文档 (6 public + 6 internal)
```

---

## 架构

```mermaid
flowchart TD
    U["👤 用户代码"] --> API["Python API — 5 客户端 + 4 Reader"]
    API --> B["PyO3 绑定层"]
    B --> NET["Net 网络层 — 连接池 / 独立连接 / 异步"]
    B --> RDR["Reader 解析层 — 日线 / 分钟线 / 板块 / 财务"]
    B --> FUND["Fund 基金层 — ETF / LOF / REITs / 分级"]
    NET --> P["Protocol 协议层 — 11 解析器 + 复权算法"]
    RDR --> P
    FUND --> NET
    P --> OUT1["dict / tuple / DataFrame"]

    S1[("TDX 服务器 — TCP / zlib")] -.-> NET
    S2[("本地文件 — .day / .lc5 / .dat")] -.-> RDR

    classDef user fill:#E3F2FD,stroke:#1565C0,color:#333
    classDef api fill:#3776AB,stroke:#2D5F8A,color:#fff
    classDef bind fill:#6C757D,stroke:#495057,color:#fff
    classDef core fill:#FF6B35,stroke:#CC5529,color:#fff
    classDef out fill:#E8F5E9,stroke:#2E7D32,color:#333
    classDef src fill:#FFF8E1,stroke:#F9A825,color:#333

    class U user
    class API api
    class B bind
    class NET,RDR,P,FUND core
    class OUT1 out
    class S1,S2 src
```

**客户端**：`TdxHqClient`（连接池 + 心跳 + 重试）、`TdxHqFundClient`（基金专用）、`TdxDirectClient`（独立连接，高并发）、`AsyncTdxHqClient`（tokio 异步）

**Reader**：`DailyBarReader`（.day 日线）、`MinBarReader` / `LcMinBarReader`（.lc5 分钟线）、`BlockReader`（.dat 板块）、`FinancialReader`（gpcw 财务）

**输出格式**：`list[dict]`（调试）、`list[tuple]`（遍历，快 40-60%）、`DataFrame`（分析回测）

> 📖 详细架构说明见 [ARCHITECTURE.md](docs/public/ARCHITECTURE.md)

---

## 文档

| 文档 | 说明 |
|------|------|
| [API 参考](docs/public/API_REFERENCE.md) | 完整 Python API + 最佳实践 |
| [架构说明](docs/public/ARCHITECTURE.md) | 模块设计、数据流、客户端策略 |
| [性能基准](docs/public/BENCHMARKS.md) | 顺序/并发性能 + 场景选择指南 |
| [CLI 指南](docs/public/CLI.md) | 命令行工具使用说明 |
| [基金模块](docs/public/FUND.md) | 基金数据 (ETF/LOF/REITs/分级基金) |
| [复权算法](docs/ADJUSTER_ALGORITHM.md) | 公式推导、版本迭代、验证方法 |
| [变更日志](docs/public/CHANGELOG.md) | 版本历史 |
| [贡献指南](docs/public/CONTRIBUTING.md) | 参与开发 + 可贡献方向 |
| [安装说明](docs/INSTALL.md) | 环境配置 + FAQ |

---

## 要求

- **Rust** 1.83+ | **Python** 3.11+ | **maturin** 1.5+
- pandas (可选, DataFrame 输出)

---

## 免责声明

- 本项目仅供**学习和研究**用途，不构成任何投资建议
- 本项目不保证数据的准确性、完整性和时效性
- 通达信行情数据的版权归相关数据提供商所有
- 用户使用本项目获取的数据用于商业用途时，需自行解决数据授权合规问题
- 本项目按 [MIT License](LICENSE) 发布，作者不对因使用本项目产生的任何损失承担责任

---

## 许可证

MIT License — 详见 [LICENSE](LICENSE)

---

## Star History

<a href="https://star-history.dera.page/#jiangtaovan/tdxrs&type=Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://star-history.dera.page/svg?repos=jiangtaovan/tdxrs&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://star-history.dera.page/svg?repos=jiangtaovan/tdxrs&type=Date" />
   <img alt="Star History Chart" src="https://star-history.dera.page/svg?repos=jiangtaovan/tdxrs&type=Date" />
 </picture>
</a>
