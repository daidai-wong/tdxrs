//! 0x051d 实时分时解析回归测试
//!
//! fixture 为 2026-09-23 盘后抓取的真实服务器报文 (2026-07 协议变更后的新格式),
//! 解析结果已与历史分时 API + 本机 vipdoc 交叉验证 (240 条全量比对)。

use tdxrs::protocol::parsers::parse_minute_time_data;

fn fixture(name: &str) -> Vec<u8> {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(name);
    std::fs::read(&path).unwrap_or_else(|e| panic!("read {} failed: {}", path.display(), e))
}

#[test]
fn test_051d_600519() {
    let body = fixture("mtd_600519.bin");
    let data = parse_minute_time_data(&body, 1, "600519").expect("parse failed");
    assert_eq!(data.len(), 240);
    // 顺时序: 09:31 -> 15:00
    assert_eq!(data[0].time, "09:31");
    assert_eq!(data[119].time, "11:30");
    assert_eq!(data[120].time, "13:01");
    assert_eq!(data[239].time, "15:00");
    // 价格锚点 (与历史分时 API 全量一致)
    assert!((data[0].price - 1256.75).abs() < 0.005, "first={}", data[0].price);
    assert!((data[239].price - 1251.24).abs() < 0.005, "last={}", data[239].price);
    // 量锚点
    assert!((data[0].vol - 634.0).abs() < 0.5);
    assert!((data[239].vol - 427.0).abs() < 0.5);
    // 均价合理 (收盘均价介于当日最低/最高之间)
    let avg = data[239].avg_price;
    assert!(avg > 1240.0 && avg < 1272.0, "avg={}", avg);
}

#[test]
fn test_051d_000001() {
    let body = fixture("mtd_000001.bin");
    let data = parse_minute_time_data(&body, 0, "000001").expect("parse failed");
    assert_eq!(data.len(), 240);
    assert!((data[0].price - 11.68).abs() < 0.005, "first={}", data[0].price);
    assert!((data[239].price - 11.60).abs() < 0.005, "last={}", data[239].price);
    assert!((data[239].vol - 7169.0).abs() < 0.5);
}

#[test]
fn test_051d_preopen_placeholder() {
    // 盘前占位报文 (count=1): 服务器开盘前重置当日数据, 只剩 1 条占位记录
    // 600519 占位价 = 昨收 1251.24; 000001 = 11.35 (服务器下发, 疑似除权参考价)
    let body = fixture("mtd1_600519.bin");
    let data = parse_minute_time_data(&body, 1, "600519").expect("parse failed");
    assert_eq!(data.len(), 1);
    assert!((data[0].price - 1251.24).abs() < 0.005, "p={}", data[0].price);
    assert_eq!(data[0].vol, 0.0);

    let body = fixture("mtd1_000001.bin");
    let data = parse_minute_time_data(&body, 0, "000001").expect("parse failed");
    assert_eq!(data.len(), 1);
    assert!((data[0].price - 11.35).abs() < 0.005, "p={}", data[0].price);
}

#[test]
fn test_051d_short_body() {
    // 空报文 / 过短报文应报错而非 panic
    assert!(parse_minute_time_data(&[], 1, "600519").is_err());
    assert!(parse_minute_time_data(&[0xf0, 0x00], 1, "600519").is_err());
    // count=0 返回空
    let empty = vec![0x00, 0x00, 0x00, 0x00, 0x01, b'6', b'0', b'0', b'5', b'1', b'9', 0x00, 0x00];
    let data = parse_minute_time_data(&empty, 1, "600519").expect("count=0 should be Ok");
    assert!(data.is_empty());
}
