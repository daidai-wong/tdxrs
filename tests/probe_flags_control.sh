#!/usr/bin/env bash
# A/B 的对照组: 同一二进制**连续**跑三次, 用来建立「跑间噪声地板」。
#
# 为什么必须有这一步: 编译参数 A/B 得到「配置间差异 <=2%」这种结论时, 必须回答
# 「2% 是配置差异还是测量抖动」。本机实测同一二进制三次连跑的噪声是
#   +59% (1_fs_read_only, IO 抖动) / ±12% (3_decode_columns, 计算核)
# —— 比配置间差异大一个数量级, 因此那些参数在本工作负载上**无法证明有收益**。
#
# 这也是本项目第三次踩「冷热 / 顺序偏差」: 首次测 Feather 写入得 864.6 ms,
# 交替 4 轮后为 37.1 ms (23x 假象)。凡涉及新分配 / 新文件的测量, 一律交替轮次取最优。
#
# 用法: bash tests/probe_flags_control.sh
set -u
cd "$(dirname "$0")/.." || exit 1
export PATH="${CARGO_HOME:-$HOME/.cargo}/bin:$PATH"
: "${PYO3_PYTHON:=$(pwd)/.venv/Scripts/python.exe}"
export PYO3_PYTHON
export VIRTUAL_ENV="$(pwd)/.venv"
OUT="tests/flags"
mkdir -p "$OUT"
CRIT="--noplot --warm-up-time 1 --measurement-time 3 --sample-size 30 local_parse_micro"

# 第一次带重建 (把 target 从 panic=abort 还原成基线), 后两次不再重建
CARGO_PROFILE_BENCH_PANIC=unwind cargo bench --bench reader_bench -- $CRIT > "$OUT/base_r2.txt" 2>&1
cargo bench --bench reader_bench -- $CRIT > "$OUT/base_r3.txt" 2>&1
cargo bench --bench reader_bench -- $CRIT > "$OUT/base_r4.txt" 2>&1

for f in base_r2 base_r3 base_r4; do
  echo "########## $f ##########"
  grep -E "^local_parse_micro/|^[[:space:]]+time:" "$OUT/$f.txt" \
    | paste - - | sed 's/[[:space:]]\+/ /g'
done
echo "FLAGS_CONTROL_DONE"
