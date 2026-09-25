use std::fs;

fn golden_dir() -> std::path::PathBuf {
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("golden")
}

fn fixtures_dir() -> std::path::PathBuf {
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests").join("fixtures")
}

#[test]
fn test_daily_bar_600519() {
    let path = fixtures_dir().join("600519.day");
    let data = fs::read(&path).expect("Failed to read fixture");
    let records = tdxrs::reader::daily_bar::parse_daily_bar(&data, 0.01).expect("Parse failed");

    // Load golden reference
    let golden_path = golden_dir().join("bars_600519_cat9_日K线.json");
    let golden_data: Vec<serde_json::Value> =
        serde_json::from_str(&fs::read_to_string(&golden_path).expect("Read golden")).unwrap();

    // Filter valid golden records (matching our fixture filter)
    let valid_golden: Vec<_> = golden_data
        .iter()
        .filter(|b| {
            let year = b["year"].as_u64().unwrap_or(0);
            let month = b["month"].as_u64().unwrap_or(0);
            let day = b["day"].as_u64().unwrap_or(0);
            let volume = b["volume"].as_f64().unwrap_or(0.0);
            let amount = b["amount"].as_f64().unwrap_or(0.0);
            let open = b["open"].as_f64().unwrap_or(0.0);

            year >= 1990
                && year <= 2100
                && month >= 1
                && month <= 12
                && day >= 1
                && day <= 31
                && volume >= 0.0
                && volume <= 4294967295.0
                && amount >= 0.0
                && amount <= 1e15
                && open > 0.0
                && open <= 100000.0
        })
        .collect();

    assert_eq!(records.len(), valid_golden.len());

    for (i, (record, golden)) in records.iter().zip(valid_golden.iter()).enumerate() {
        let g_year = golden["year"].as_u64().unwrap();
        let g_month = golden["month"].as_u64().unwrap();
        let g_day = golden["day"].as_u64().unwrap();

        assert_eq!(record.year, g_year as u32, "[{i}] year mismatch");
        assert_eq!(record.month, g_month as u32, "[{i}] month mismatch");
        assert_eq!(record.day, g_day as u32, "[{i}] day mismatch");

        let g_open = golden["open"].as_f64().unwrap();
        assert!(
            (record.open - g_open).abs() < 0.02,
            "[{i}] open mismatch: {} vs {}",
            record.open,
            g_open
        );

        let g_volume = golden["volume"].as_f64().unwrap();
        assert!(
            (record.volume - g_volume).abs() < 1.0,
            "[{i}] volume mismatch: {} vs {}",
            record.volume,
            g_volume
        );
    }
}

#[test]
fn test_min_bar_600519() {
    let path = fixtures_dir().join("600519.lc5");
    let data = fs::read(&path).expect("Failed to read fixture");
    let records = tdxrs::reader::min_bar::parse_lc_min_bar(&data).expect("Parse failed");

    assert!(!records.is_empty());

    // golden: 由 gen_fixtures_local.py 从真实 vipdoc 分钟线生成 (fixture/golden 同源)
    let golden_path = golden_dir().join("bars_600519_cat0_5分钟线.json");
    let golden: Vec<serde_json::Value> =
        serde_json::from_str(&fs::read_to_string(&golden_path).expect("Read golden")).unwrap();
    assert_eq!(records.len(), golden.len(), "record count mismatch");

    let r0 = &records[0];
    let g0 = &golden[0];
    assert_eq!(r0.year, g0["year"].as_u64().unwrap() as u32, "year mismatch");
    assert_eq!(r0.month, g0["month"].as_u64().unwrap() as u32, "month mismatch");
    assert_eq!(r0.day, g0["day"].as_u64().unwrap() as u32, "day mismatch");
    let g_open = g0["open"].as_f64().unwrap();
    assert!(
        (r0.open - g_open).abs() < 0.02,
        "open mismatch: {} vs {}",
        r0.open,
        g_open
    );
}

#[test]
fn test_block_reader() {
    let path = fixtures_dir().join("test_block.dat");
    let data = fs::read(&path).expect("Failed to read fixture");
    let records = tdxrs::reader::block::parse_block(&data).expect("Parse failed");

    assert_eq!(records.len(), 5);
    assert_eq!(records[0].code, "600000");
    assert_eq!(records[1].code, "000001");
    assert_eq!(records[2].code, "300750");
    assert_eq!(records[3].code, "000001");
    assert_eq!(records[4].code, "399001");
}

#[test]
fn test_financial_reader() {
    let path = fixtures_dir().join("test_finance.dat");
    let data = fs::read(&path).expect("Failed to read fixture");
    let records = tdxrs::reader::financial::parse_financial(&data).expect("Parse failed");

    assert_eq!(records.len(), 2);
    assert_eq!(records[0].code, "600519");
    assert_eq!(records[0].report_date, 20241231);
    assert_eq!(records[0].fields.len(), 4);
    assert!((records[0].fields[0] - 1835.0).abs() < 0.01);
}
