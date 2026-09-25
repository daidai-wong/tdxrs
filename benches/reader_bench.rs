//! 本地解析微基准 — 真实 vipdoc 数据
//!
//! 目的: 把「本地 .day 解析」拆成各环节并量化
//!   1. fs_read_only         —— 纯文件 IO (std::fs::read)
//!   2. decode_current       —— 现库实现 (Vec<Record> + 每条 String date)
//!   3. decode_columns       —— 原型: 直接列式 (无中间 struct / 无 String)
//!   4. decode_columns_chunks—— 原型: 列式 + chunks_exact 消边界检查
//!   5. decode_columns_dates_str —— 原型: 列式 + 保留 String 日期 (对照 3)
//!   6. scan_seq / scan_par  —— 多文件扫描 (串行 vs 8 线程) 的 IO+解码扩展性
use criterion::{criterion_group, criterion_main, Criterion};
use std::fs;
use std::hint::black_box;
use std::path::PathBuf;
use std::time::Duration;

use tdxrs::constants::{decode_date, read_f32, read_u32};

const REC: usize = 32;
const VIPDOC: &str = r"D:\TDX\vipdoc";
const BIG_FILE: &str = r"D:\TDX\vipdoc\sh\lday\sh600601.day";

// ---------- 原型 A: 纯列式 (无中间 struct, 无 String) ----------
struct Columns {
    date: Vec<u32>, // 归一化 YYYYMMDD
    open: Vec<f64>,
    high: Vec<f64>,
    low: Vec<f64>,
    close: Vec<f64>,
    amount: Vec<f64>,
    volume: Vec<f64>,
}

fn decode_columns(data: &[u8], coef: f64) -> Columns {
    let n = data.len() / REC;
    let mut c = Columns {
        date: Vec::with_capacity(n),
        open: Vec::with_capacity(n),
        high: Vec::with_capacity(n),
        low: Vec::with_capacity(n),
        close: Vec::with_capacity(n),
        amount: Vec::with_capacity(n),
        volume: Vec::with_capacity(n),
    };
    for i in 0..n {
        let o = i * REC;
        let (y, m, d) = decode_date(read_u32(data, o));
        c.date.push(y * 10000 + m * 100 + d);
        c.open.push(read_u32(data, o + 4) as f64 * coef);
        c.high.push(read_u32(data, o + 8) as f64 * coef);
        c.low.push(read_u32(data, o + 12) as f64 * coef);
        c.close.push(read_u32(data, o + 16) as f64 * coef);
        c.amount.push(read_f32(data, o + 20) as f64);
        c.volume.push(read_u32(data, o + 24) as f64);
    }
    c
}

// ---------- 原型 B: 列式 + chunks_exact (消除每次读的边界检查) ----------
#[inline(always)]
fn u32_at(b: &[u8], i: usize) -> u32 {
    u32::from_le_bytes([b[i], b[i + 1], b[i + 2], b[i + 3]])
}
#[inline(always)]
fn f32_at(b: &[u8], i: usize) -> f32 {
    f32::from_le_bytes([b[i], b[i + 1], b[i + 2], b[i + 3]])
}

fn decode_columns_chunks(data: &[u8], coef: f64) -> Columns {
    let n = data.len() / REC;
    let mut c = Columns {
        date: Vec::with_capacity(n),
        open: Vec::with_capacity(n),
        high: Vec::with_capacity(n),
        low: Vec::with_capacity(n),
        close: Vec::with_capacity(n),
        amount: Vec::with_capacity(n),
        volume: Vec::with_capacity(n),
    };
    for rec in data.chunks_exact(REC) {
        let (y, m, d) = decode_date(u32_at(rec, 0));
        c.date.push(y * 10000 + m * 100 + d);
        c.open.push(u32_at(rec, 4) as f64 * coef);
        c.high.push(u32_at(rec, 8) as f64 * coef);
        c.low.push(u32_at(rec, 12) as f64 * coef);
        c.close.push(u32_at(rec, 16) as f64 * coef);
        c.amount.push(f32_at(rec, 20) as f64);
        c.volume.push(u32_at(rec, 24) as f64);
    }
    c
}

// ---------- 原型 C: 列式但保留 String 日期 (测 String 分配成本) ----------
fn decode_columns_dates_str(data: &[u8], coef: f64) -> (Columns, Vec<String>) {
    let n = data.len() / REC;
    let mut c = decode_columns_chunks(data, coef);
    let mut dates = Vec::with_capacity(n);
    for i in 0..n {
        let (y, m, d) = decode_date(read_u32(data, i * REC));
        dates.push(format!("{:04}-{:02}-{:02}", y, m, d));
    }
    c.date.clear();
    (c, dates)
}

fn checksum(c: &Columns) -> f64 {
    c.close.iter().sum::<f64>() + c.volume.iter().sum::<f64>() + c.date.len() as f64
}

fn scan_seq(files: &[PathBuf], coef: f64) -> f64 {
    let mut acc = 0.0;
    for f in files {
        if let Ok(d) = fs::read(f) {
            acc += checksum(&decode_columns_chunks(&d, coef));
        }
    }
    acc
}

fn scan_par(files: &[PathBuf], coef: f64, nthreads: usize) -> f64 {
    let chunk = files.len().div_ceil(nthreads);
    let mut total = 0.0;
    std::thread::scope(|s| {
        let handles: Vec<_> = files
            .chunks(chunk)
            .map(|part| s.spawn(move || scan_seq(part, coef)))
            .collect();
        for h in handles {
            total += h.join().unwrap();
        }
    });
    total
}

fn load_files(limit: usize) -> Vec<PathBuf> {
    let d = PathBuf::from(VIPDOC).join("sh").join("lday");
    let mut v: Vec<PathBuf> = fs::read_dir(d)
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().map(|x| x == "day").unwrap_or(false))
        .collect();
    v.sort();
    v.truncate(limit);
    v
}

fn bench_micro(c: &mut Criterion) {
    let data = fs::read(BIG_FILE).unwrap();
    let bars = data.len() / REC;
    println!(
        "\n== 单文件: {} ({} bars, {} KB) ==\n",
        BIG_FILE,
        bars,
        data.len() / 1024
    );

    let mut g = c.benchmark_group("local_parse_micro");
    g.bench_function("1_fs_read_only", |b| {
        b.iter(|| black_box(fs::read(BIG_FILE).unwrap().len()))
    });
    g.bench_function("2_decode_current", |b| {
        b.iter(|| {
            black_box(
                tdxrs::reader::daily_bar::parse_daily_bar(&data, 0.01)
                    .unwrap()
                    .len(),
            )
        })
    });
    g.bench_function("3_decode_columns", |b| {
        b.iter(|| black_box(checksum(&decode_columns(&data, 0.01))))
    });
    g.bench_function("4_decode_columns_chunks", |b| {
        b.iter(|| black_box(checksum(&decode_columns_chunks(&data, 0.01))))
    });
    g.bench_function("5_decode_columns_dates_str", |b| {
        b.iter(|| black_box(decode_columns_dates_str(&data, 0.01).1.len()))
    });
    g.finish();
}

fn bench_scan(c: &mut Criterion) {
    let files = load_files(300);
    let bytes: usize = files.iter().map(|f| fs::metadata(f).unwrap().len() as usize).sum();
    println!(
        "\n== 多文件扫描: {} 文件, {} MB ==\n",
        files.len(),
        bytes / 1024 / 1024
    );

    let mut g = c.benchmark_group("local_parse_scan");
    g.sample_size(10)
        .measurement_time(Duration::from_secs(10))
        .warm_up_time(Duration::from_secs(3));
    g.bench_function("io_seq_read_only", |b| {
        b.iter(|| {
            let mut n = 0usize;
            for f in &files {
                n += fs::read(f).unwrap().len();
            }
            black_box(n)
        })
    });
    g.bench_function("decode_seq_cols", |b| {
        b.iter(|| black_box(scan_seq(&files, 0.01)))
    });
    g.bench_function("decode_par8_cols", |b| {
        b.iter(|| black_box(scan_par(&files, 0.01, 8)))
    });
    g.bench_function("decode_par16_cols", |b| {
        b.iter(|| black_box(scan_par(&files, 0.01, 16)))
    });
    g.finish();
}

criterion_group!(micro, bench_micro);
criterion_group!(scan, bench_scan);
criterion_main!(micro, scan);
