# 性能基线（BENCHMARKS_BASELINE）

> 本文件是重构期间的性能回归裁判。任何改动若导致下列指标退化 >10%，阻断合并。
> 记录环境：Windows 11 / Rust 1.96.0 (release+LTO) / Python 3.13.12 / pandas 3.0.6
> 复跑命令：`python examples/bench_hotpath.py --json bench.json`（best-of-5）

## 基线记录（2026-09-23，optimization 分支 @ P0 修复后，20 万条 .day）

| 模式 | 吞吐 (bars/s) | 耗时 (µs/bar) | 内存 (B/bar, 10k 缩样) |
|------|--------------:|--------------:|----------------------:|
| parse_data (list[dict]) | ~580,000 | 1.72 | 958 |
| parse_data_tuples (list[tuple]) | ~1,320,000 | 0.76 | 352 |
| to_dataframe (pandas) | ~520,000 | 1.92 | — |

*注：绝对值随机器负载波动 ±40%，以同进程内相对比值与退化比例为准；对比时基线与新版同环境复跑。*

## 网络 RTT 基线（get_security_count 实测，2026-09-23）

| 项 | 数值 |
|----|-----:|
| 冷调用（真实往返） | 46.5 ms |
| 缓存命中（30s TTL） | ≈ 0 ms |
| 前复权请求成本模型 | 8 RTT ≈ 372 ms（xdxr/context 无缓存） |

## 阶段目标（详见 REFACTOR_PLAN.md）

| 阶段 | 目标指标 |
|------|---------|
| 1 微优化 | tuples 吞吐 ≥ 基线 × 1.15；每 bar 堆分配 -1 |
| 2 复权缓存 | 二次 fq 请求 xdxr/context 网络调用 = 0 |
| 3 GIL 释放 | 4 线程吞吐 ≥ 单线程 × 3.0 |
| 4 列式 DF | to_dataframe 吞吐 ≥ 基线 × 4 |
