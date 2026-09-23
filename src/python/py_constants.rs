//! 协议常量暴露 — 消除 Python 端硬编码
//!
//! 在 lib.rs 中调用 `register_constants(m)?` 将所有常量注入模块。

use crate::error_codes::ErrorCode;
use crate::protocol::constants;
use pyo3::prelude::*;

/// 将协议常量注册到 Python 模块的 __dict__
pub fn register_constants(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // 市场代码
    m.setattr("MARKET_SZ", 0u8)?;
    m.setattr("MARKET_SH", 1u8)?;
    m.setattr("MARKET_BJ", 2u8)?;

    // K线种类
    m.setattr("KLINE_5MIN", constants::KLINE_5MIN)?;
    m.setattr("KLINE_15MIN", constants::KLINE_15MIN)?;
    m.setattr("KLINE_30MIN", constants::KLINE_30MIN)?;
    m.setattr("KLINE_1HOUR", constants::KLINE_1HOUR)?;
    m.setattr("KLINE_DAILY", constants::KLINE_DAILY)?;
    m.setattr("KLINE_WEEKLY", constants::KLINE_WEEKLY)?;
    m.setattr("KLINE_MONTHLY", constants::KLINE_MONTHLY)?;
    m.setattr("KLINE_EXHQ_1MIN", constants::KLINE_EXHQ_1MIN)?;
    m.setattr("KLINE_1MIN", constants::KLINE_1MIN)?;
    m.setattr("KLINE_RI_K", constants::KLINE_RI_K)?;
    m.setattr("KLINE_3MONTH", constants::KLINE_3MONTH)?;
    m.setattr("KLINE_YEARLY", constants::KLINE_YEARLY)?;

    // 复权类型
    m.setattr("FQ_NONE", constants::fq_type::NONE)?;
    m.setattr("FQ_QFQ", constants::fq_type::QFQ)?;
    m.setattr("FQ_HFQ", constants::fq_type::HFQ)?;

    // 限制
    m.setattr("MAX_KLINE_COUNT", constants::MAX_KLINE_COUNT)?;
    m.setattr("MAX_TRANSACTION_COUNT", constants::MAX_TRANSACTION_COUNT)?;

    // 默认配置
    m.setattr("DEFAULT_PORT", constants::DEFAULT_PORT)?;
    m.setattr("DEFAULT_POOL_SIZE", constants::DEFAULT_POOL_SIZE)?;
    m.setattr("FQ_PRICE_PRECISION", constants::FQ_PRICE_PRECISION)?;

    // 主力服务器列表 — Python 侧服务器选择的单一事实来源
    // (downloader.py / cli.py 均由此取值, 不再各自硬编码过期副本)
    let primary_servers: Vec<(String, String, u16)> = constants::PRIMARY_SERVERS
        .iter()
        .map(|(name, ip, port)| (name.to_string(), ip.to_string(), *port))
        .collect();
    m.setattr("PRIMARY_SERVERS", primary_servers)?;

    // 全量已知服务器 (101 唯一 IP) — 供 Python 层注入 IP 池:
    // Downloader(servers=ALL_KNOWN_SERVERS) / HybridClient(servers=...) / 自定义均衡
    let all_known_servers: Vec<(String, String, u16)> = constants::ALL_KNOWN_SERVERS
        .iter()
        .map(|(name, ip, port)| (name.to_string(), ip.to_string(), *port))
        .collect();
    m.setattr("ALL_KNOWN_SERVERS", all_known_servers)?;

    // 别名 (向后兼容)
    m.setattr("PORT", constants::DEFAULT_PORT)?;
    m.setattr("POOL_SIZE", constants::DEFAULT_POOL_SIZE)?;

    // 板块文件名
    m.setattr("BLOCK_SZ", constants::BLOCK_SZ)?;
    m.setattr("BLOCK_FG", constants::BLOCK_FG)?;
    m.setattr("BLOCK_GN", constants::BLOCK_GN)?;
    m.setattr("BLOCK_DEFAULT", constants::BLOCK_DEFAULT)?;

    // 错误码 — 通用
    m.setattr("ERR_EMPTY_ARGUMENT", ErrorCode::EMPTY_ARGUMENT.0)?;
    m.setattr("ERR_ARGUMENT_TOO_LONG", ErrorCode::ARGUMENT_TOO_LONG.0)?;
    m.setattr("ERR_ARGUMENT_OUT_OF_RANGE", ErrorCode::ARGUMENT_OUT_OF_RANGE.0)?;
    m.setattr("ERR_INVALID_FORMAT", ErrorCode::INVALID_FORMAT.0)?;

    // 错误码 — 代码分类
    m.setattr("ERR_BLOCK_CODE_IN_GENERAL_CLIENT", ErrorCode::BLOCK_CODE_IN_GENERAL_CLIENT.0)?;
    m.setattr("ERR_BOND_CODE_NOT_SUPPORTED", ErrorCode::BOND_CODE_NOT_SUPPORTED.0)?;
    m.setattr("ERR_FUND_CODE_NOT_SUPPORTED", ErrorCode::FUND_CODE_NOT_SUPPORTED.0)?;
    m.setattr("ERR_UNKNOWN_CODE_FORMAT", ErrorCode::UNKNOWN_CODE_FORMAT.0)?;

    // 错误码 — 限流
    m.setattr("ERR_RATE_LIMIT_EXCEEDED", ErrorCode::RATE_LIMIT_EXCEEDED.0)?;
    m.setattr("ERR_RATE_LIMIT_DAILY_EXCEEDED", ErrorCode::RATE_LIMIT_DAILY_EXCEEDED.0)?;
    m.setattr("ERR_RATE_LIMIT_MINUTE_EXCEEDED", ErrorCode::RATE_LIMIT_MINUTE_EXCEEDED.0)?;
    m.setattr("ERR_BLOCK_KLINE_CATEGORY_NOT_ALLOWED", ErrorCode::BLOCK_KLINE_CATEGORY_NOT_ALLOWED.0)?;
    m.setattr("ERR_BLOCK_KLINE_COUNT_EXCEEDED", ErrorCode::BLOCK_KLINE_COUNT_EXCEEDED.0)?;
    m.setattr("ERR_BLOCK_MINUTE_DISABLED", ErrorCode::BLOCK_MINUTE_DISABLED.0)?;
    m.setattr("ERR_BLOCK_TRADES_DISABLED", ErrorCode::BLOCK_TRADES_DISABLED.0)?;

    // 错误码 — 连接
    m.setattr("ERR_CONNECTION_FAILED", ErrorCode::CONNECTION_FAILED.0)?;
    m.setattr("ERR_CONNECTION_TIMEOUT", ErrorCode::CONNECTION_TIMEOUT.0)?;
    m.setattr("ERR_DISCONNECTED", ErrorCode::DISCONNECTED.0)?;
    m.setattr("ERR_HANDSHAKE_FAILED", ErrorCode::HANDSHAKE_FAILED.0)?;
    m.setattr("ERR_RETRY_EXHAUSTED", ErrorCode::RETRY_EXHAUSTED.0)?;
    m.setattr("ERR_POOL_EXHAUSTED", ErrorCode::POOL_EXHAUSTED.0)?;

    // 错误码 — 解析
    m.setattr("ERR_INVALID_DATE", ErrorCode::INVALID_DATE.0)?;
    m.setattr("ERR_DATE_OUT_OF_RANGE", ErrorCode::DATE_OUT_OF_RANGE.0)?;
    m.setattr("ERR_INVALID_STOCK_CODE", ErrorCode::INVALID_STOCK_CODE.0)?;
    m.setattr("ERR_MISSING_FIELD", ErrorCode::MISSING_FIELD.0)?;
    m.setattr("ERR_TYPE_MISMATCH", ErrorCode::TYPE_MISMATCH.0)?;

    // 错误码 — 文件
    m.setattr("ERR_FILE_NOT_FOUND", ErrorCode::FILE_NOT_FOUND.0)?;
    m.setattr("ERR_INVALID_FILE_FORMAT", ErrorCode::INVALID_FILE_FORMAT.0)?;
    m.setattr("ERR_FILE_TOO_SMALL", ErrorCode::FILE_TOO_SMALL.0)?;
    m.setattr("ERR_FILE_READ_ERROR", ErrorCode::FILE_READ_ERROR.0)?;

    Ok(())
}
