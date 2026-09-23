//! 基金模块
//!
//! 提供基金 (ETF、LOF、REITs、分级基金等) 数据的解析和获取功能。
//!
//! # 概述
//!
//! 基金在通达信中与股票共享市场代码，通过代码前缀区分：
//! - 沪市基金: 50xxxx, 51xxxx (market=1)
//! - 深市基金: 15xxxx, 16xxxx (market=0)
//!
//! 本模块封装了现有的行情客户端，提供基金专用的 API，
//! 自动处理代码验证和系数转换。
//!
//! # 基金类型
//!
//! | 类型 | 代码前缀 | 说明 |
//! |------|---------|------|
//! | ETF | 510/512/513/515/516/159 | 交易型开放式指数基金 |
//! | LOF | 501/502/160/161 | 上市型开放式基金 |
//! | REITs | 508 | 不动产投资信托基金 |
//! | 分级 | 162/163/164 | 结构化基金 |
//! | 债券 | 511 | 债券 ETF |
//! | 开放 | 519 | 传统开放式基金 |
//!
//! # 示例
//!
//! ```no_run
//! use tdxrs::fund::client::TdxHqFundClient;
//! use tdxrs::fund::constants::{MARKET_SH, MARKET_SZ, FundType};
//! use tdxrs::protocol::constants::KLINE_DAILY;
//!
//! let client = TdxHqFundClient::new();
//! client.connect_to_any(None).unwrap();
//!
//! // 获取基金列表
//! let funds = client.get_fund_list(MARKET_SH).unwrap();
//! for fund in &funds {
//!     println!("{}: {} ({})", fund.code, fund.name, fund.fund_type.name());
//! }
//!
//! // 获取 ETF K线
//! let bars = client.get_fund_bars(KLINE_DAILY, MARKET_SH, "510300", 0, 100).unwrap();
//! for bar in &bars {
//!     println!("{}: O={:.3} C={:.3}", bar.datetime, bar.open, bar.close);
//! }
//!
//! // 获取基金实时行情
//! let quotes = client.get_fund_quotes(&[
//!     (MARKET_SH, "510300"),
//!     (MARKET_SZ, "159915"),
//! ]).unwrap();
//!
//! for q in &quotes {
//!     println!("{}: price={:.3}", q.code, q.price);
//! }
//! ```

pub mod client;
pub mod constants;
pub mod types;
pub mod utils;

// 重新导出常用类型
pub use client::TdxHqFundClient;
pub use constants::*;
pub use types::*;
pub use utils::*;
