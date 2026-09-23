# -*- coding: utf-8 -*-
"""从本机真实通达信 vipdoc 数据生成 Rust Reader 回归测试的 fixture + golden

替代上游 gen_binary_fixtures.py (依赖 tdxpy/test_data/golden, 本机不存在)。
数据源: D:\\TDX\\vipdoc (官方客户端数据, 已与服务器交叉验证 0 mismatch)。

产出:
  tests/fixtures/600519.day                        <- 真实日线尾部 250 条
  tests/fixtures/600519.lc5                        <- 真实 1 分钟线尾部 100 条
                                                     (lc1/lc5 同为 32 字节记录格式,
                                                      对 parse_lc_min_bar 等价)
  tests/fixtures/test_block.dat                    <- 构造板块文件 (同上游)
  tests/fixtures/test_finance.dat                  <- 构造财务文件 (同上游)
  tests/golden/bars_600519_cat9_日K线.json          <- 日线 golden
  tests/golden/bars_600519_cat0_5分钟线.json        <- 分钟线 golden
"""
import json
import struct
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
FIXTURE_DIR = SCRIPT_DIR / "fixtures"
GOLDEN_DIR = SCRIPT_DIR / "golden"

VIPDOC = Path(r"D:\TDX\vipdoc")
DAY_SRC = VIPDOC / "sh" / "lday" / "sh600519.day"
MIN_SRC = VIPDOC / "sh" / "minline" / "sh600519.lc1"

DAY_TAIL = 250    # 日线取尾部条数
MIN_TAIL = 100    # 分钟线取尾部条数


def decode_date(date_num: int):
    """TDX 日期解码 (与 Rust decode_date 一致):
    >100000 -> YYYYMMDD 整数 (官方新版 vipdoc .day 实际格式)
    否则    -> TDX 压缩编码 (year-2004)*2048 + month*100 + day
    """
    if date_num > 100000:
        return date_num // 10000, (date_num % 10000) // 100, date_num % 100
    year = date_num // 2048 + 2004
    rest = date_num % 2048
    return year, rest // 100, rest % 100


def gen_day():
    raw = DAY_SRC.read_bytes()
    assert len(raw) % 32 == 0, f".day 大小非 32 倍数: {len(raw)}"
    tail = raw[-DAY_TAIL * 32:]
    FIXTURE_DIR.mkdir(exist_ok=True)
    (FIXTURE_DIR / "600519.day").write_bytes(tail)

    golden = []
    for off in range(0, len(tail), 32):
        date_num, o, h, l, c, amount, volume, _ = struct.unpack_from("<IIIIIfII", tail, off)
        year, month, day = decode_date(date_num)
        golden.append({
            "year": year, "month": month, "day": day,
            "open": o / 100, "high": h / 100, "low": l / 100, "close": c / 100,
            "volume": float(volume), "amount": amount,
        })
    GOLDEN_DIR.mkdir(exist_ok=True)
    out = GOLDEN_DIR / "bars_600519_cat9_日K线.json"
    out.write_text(json.dumps(golden, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"600519.day: {DAY_TAIL} 条 ({DAY_SRC.name} 尾部), golden -> {out.name}")
    print(f"  首条 {golden[0]['year']}-{golden[0]['month']:02d}-{golden[0]['day']:02d} "
          f"O={golden[0]['open']}  末条 {golden[-1]['year']}-{golden[-1]['month']:02d}-{golden[-1]['day']:02d} "
          f"C={golden[-1]['close']}")


def gen_min():
    raw = MIN_SRC.read_bytes()
    assert len(raw) % 32 == 0, f".lc1 大小非 32 倍数: {len(raw)}"
    tail = raw[-MIN_TAIL * 32:]
    (FIXTURE_DIR / "600519.lc5").write_bytes(tail)

    golden = []
    for off in range(0, len(tail), 32):
        # 真实官方 .lc1/.lc5 浮点格式: date(u16), time(u16), OHLC f32 x4,
        # amount(f32), volume(u32), reserved(u32)  —— 对应 parse_lc_min_bar
        date_num, time_num, o, h, l, c, amount, volume, _ = struct.unpack_from(
            "<HHfffffII", tail, off)
        year, month, day = decode_date(date_num)
        golden.append({
            "year": year, "month": month, "day": day,
            "hour": time_num // 60, "minute": time_num % 60,
            "open": float(o), "high": float(h), "low": float(l), "close": float(c),
            "volume": float(volume), "amount": float(amount),
        })
    out = GOLDEN_DIR / "bars_600519_cat0_5分钟线.json"
    out.write_text(json.dumps(golden, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"600519.lc5: {MIN_TAIL} 条 ({MIN_SRC.name} 尾部), golden -> {out.name}")
    g0 = golden[0]
    print(f"  首条 {g0['year']}-{g0['month']:02d}-{g0['day']:02d} "
          f"{g0['hour']:02d}:{g0['minute']:02d} O={g0['open']}")


def gen_block():
    """构造板块测试文件 (逻辑同上游 gen_binary_fixtures.py)"""
    block_file = FIXTURE_DIR / "test_block.dat"
    header = b'\x00' * 384
    num_blocks = 2
    block1_name = "测试板块".encode("gbk").ljust(9, b'\x00')
    codes1_block = b''.join([b'600000\x00', b'000001\x00', b'300750\x00']).ljust(2800, b'\x00')
    block2_name = "指数板块".encode("gbk").ljust(9, b'\x00')
    codes2_block = b''.join([b'000001\x00', b'399001\x00']).ljust(2800, b'\x00')
    with open(block_file, "wb") as out:
        out.write(header)
        out.write(struct.pack("<H", num_blocks))
        out.write(block1_name)
        out.write(struct.pack("<HH", 3, 2))   # block_type=2: parse_block 只解析 type==2
        out.write(codes1_block)
        out.write(block2_name)
        out.write(struct.pack("<HH", 2, 2))
        out.write(codes2_block)
    print(f"test_block.dat: {num_blocks} 板块 5 股票, {block_file.stat().st_size} bytes")


def gen_financial():
    """构造财务测试文件 (逻辑同上游 gen_binary_fixtures.py)"""
    fin_file = FIXTURE_DIR / "test_finance.dat"
    stocks = [
        ("600519", [1835.0, 1849.98, 1807.82, 1841.2]),
        ("000858", [150.5, 155.0, 148.0, 152.3]),
    ]
    report_size = len(stocks[0][1]) * 4
    header = struct.pack("<hI1H3L", 1, 20241231, len(stocks), 0, report_size, 0)
    header_size, index_size = 20, 11 * len(stocks)
    with open(fin_file, "wb") as out:
        out.write(header)
        for i, (code, _) in enumerate(stocks):
            out.write(code.encode("utf-8").ljust(6, b'\x00')[:6])
            out.write(b'\x00')
            out.write(struct.pack("<I", header_size + index_size + i * report_size))
        for _, fields in stocks:
            for val in fields:
                out.write(struct.pack("<f", val))
    print(f"test_finance.dat: {len(stocks)} stocks, {fin_file.stat().st_size} bytes")


if __name__ == "__main__":
    gen_day()
    gen_min()
    gen_block()
    gen_financial()
    print("\n全部 fixture/golden 生成完成!")
