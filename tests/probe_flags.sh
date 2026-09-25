#!/usr/bin/env bash
# P5 编译参数 A/B: 用真实 vipdoc 的 Rust 解码微基准 (benches/reader_bench.rs) 比较
#   ① 现状 (opt-level=3, lto=true, codegen-units=16)
#   ② codegen-units=1     ③ target-cpu=native     ④ 两者叠加     ⑤ panic=abort
#
# 为什么必须串行: 每个配置改 RUSTFLAGS / profile 都会让整树失效并重建, 而 cargo
# 用 target 目录锁, 并发跑只会互相阻塞。单个配置约 20~110 秒。
#
# 结论 (2026-09-25, 本机): 五组配置差异 <=2%, 见 Cargo.toml [profile.release] 注释。
# 跑完请配合 tests/probe_flags_control.sh 建立「噪声地板」再下结论 ——
# 同一二进制三次连跑的噪声可达 +59% (IO) / ±12% (计算核), 远比配置差异大。
#
# 注意: 某些环境下 `env` 可能被 shim 替换并吞掉 stdout, 因此这里用 bash 内联赋值。
#
# 用法: bash tests/probe_flags.sh
set -u
cd "$(dirname "$0")/.." || exit 1
export PATH="${CARGO_HOME:-$HOME/.cargo}/bin:$PATH"
: "${PYO3_PYTHON:=$(pwd)/.venv/Scripts/python.exe}"
export PYO3_PYTHON
export VIRTUAL_ENV="$(pwd)/.venv"
OUT="tests/flags"
mkdir -p "$OUT"

CRIT="--noplot --warm-up-time 1 --measurement-time 3 --sample-size 30 local_parse_micro"

run() {
  name="$1"; prefix="$2"
  echo "########## $name   [$prefix] ##########"
  local t0=$SECONDS rc=0
  eval "$prefix cargo bench --bench reader_bench -- $CRIT" > "$OUT/$name.txt" 2>&1 || rc=$?
  echo "$name exit=$rc build+run=$((SECONDS-t0))s"
  grep -E "^local_parse_micro/|^[[:space:]]+time:" "$OUT/$name.txt" \
    | paste - - | sed 's/[[:space:]]\+/ /g'
}

run baseline      ""
run cgu1          "CARGO_PROFILE_BENCH_CODEGEN_UNITS=1"
run native        "RUSTFLAGS='-C target-cpu=native'"
run cgu1_native   "CARGO_PROFILE_BENCH_CODEGEN_UNITS=1 RUSTFLAGS='-C target-cpu=native'"
run abort         "CARGO_PROFILE_BENCH_PANIC=abort"
# 还原成基线配置, 免得把 panic=abort 留在 target 里给后续测试用
CARGO_PROFILE_BENCH_PANIC=unwind cargo bench --bench reader_bench -- $CRIT > /dev/null 2>&1
echo "FLAGS_AB_DONE"
