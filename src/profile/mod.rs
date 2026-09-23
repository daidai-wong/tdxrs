/// F10 公司资料模块
///
/// 提供通达信 F10 公司基本面资料数据的获取能力，包括：
/// - 公司概况
/// - 财务分析
/// - 股东研究
/// - 股本结构
/// - 等 16 类信息
///
/// # 示例
///
/// ```ignore
/// use tdxrs::net::client::TdxHqClient;
/// use tdxrs::profile::ProfileClient;
///
/// let mut client = TdxHqClient::new();
/// client.connect("117.34.114.14", 7709, None)?;
///
/// let mut profile = ProfileClient::new(&mut client);
/// let categories = profile.get_category(1, "600519")?;
/// for cat in &categories {
///     println!("{}: {} bytes", cat.name, cat.length);
/// }
/// # Ok::<(), Box<dyn std::error::Error>>
/// ```

pub mod client;
pub mod constants;
pub mod parser;
pub mod parser_f10;
pub mod types;

pub use client::ProfileClient;
pub use crate::net::utils::auto_market;
pub use constants::*;
pub use parser_f10::{parse_f10_text, extract_basic_info, F10Parsed, F10TextParser};
pub use types::*;
