//! 复权价格调整 — 纯算法模块
//!
//! 基于除权除息信息对 K 线价格进行前复权/后复权计算。
//! TDX 服务端 CMD_SECURITY_BARS 返回的是未复权原始数据,
//! 复权需在客户端侧完成。
//!
//! ## 公式 (中国A股标准除权除息公式)
//!
//! 除权除息日开盘参考价:
//!   P_ex = (P_close - D + P_rights × R_rights) / (1 + R_bonus + R_rights)
//!
//! 前复权因子: factor = P_ex / P_close
//!   所有除权日之前的 K 线价格 × cumulative_factor
//!
//! 后复权因子: factor = P_close / P_ex
//!   所有除权日当天及之后的 K 线价格 × cumulative_factor
//!
//! ## 参数单位
//!
//! | TDX 字段     | 单位         | 算法中使用    |
//! |-------------|-------------|-------------|
//! | fenhong     | 元/10股     | ÷10 = 元/股 |
//! | songzhuangu | 股/10股     | ÷10 = 比例  |
//! | peigu       | 股/10股     | ÷10 = 比例  |
//! | peigujia    | 元/股       | 直接使用     |

use crate::protocol::constants::FQ_PRICE_PRECISION;
use crate::protocol::types::{IndexBar, SecurityBar, XdXrInfo};
use std::collections::BTreeMap;

// ================================================================
// 复权因子计算接口 (独立于 K 线调整)
// ================================================================

/// 单个除权事件的因子详情
#[derive(Debug, Clone)]
pub struct FqFactorItem {
    /// 除权除息日期 (YYYYMMDD)
    pub date: u32,
    /// 前收盘价 (除权日前一交易日收盘价)
    pub close_before: f64,
    /// 前复权因子 (QFQ)
    pub qfq_factor: f64,
    /// 后复权因子 (HFQ = 1/QFQ)
    pub hfq_factor: f64,
    /// 分红 (元/股, 已从元/10股转换)
    pub div_per_share: f64,
    /// 送股比例 (已从股/10股转换)
    pub bonus_ratio: f64,
    /// 配股比例 (已从股/10股转换)
    pub rights_ratio: f64,
    /// 配股价 (元/股)
    pub rights_price: f64,
}

/// 复权因子计算结果
#[derive(Debug, Clone)]
pub struct FqFactorResult {
    /// 因子列表 (按日期升序)
    pub factors: Vec<FqFactorItem>,
    /// 累计前复权因子 (所有因子的乘积)
    pub cumulative_qfq: f64,
    /// 累计后复权因子 (= 1/cumulative_qfq)
    pub cumulative_hfq: f64,
}

/// 计算复权因子 (不修改 K 线数据)
///
/// 根据 XDXR 历史和上下文 K 线数据，计算并返回每个除权事件的复权因子。
/// 可用于:
/// - 验证复权精度
/// - 手动应用复权调整
/// - 导出因子表供外部使用
///
/// # 参数
///
/// - `xdxr_list`: 该股票的除权除息历史 (由 get_xdxr_info 获取)
/// - `bars`: 请求的 K 线数据 (按日期升序)
/// - `context_bars`: 额外的历史 K 线数据 (按日期升序)，用于查找除权日前收盘价
///
/// # 返回
///
/// `FqFactorResult` 包含每个事件的因子和累计因子。
/// 如果某个事件找不到前收盘价，该事件将被跳过。
pub fn calc_fq_factors(
    xdxr_list: &[XdXrInfo],
    bars: &[SecurityBar],
    context_bars: &[SecurityBar],
) -> FqFactorResult {
    let mut factors = Vec::new();

    // 1. 构建除权事件 (升序)
    let mut events: Vec<(u32, FactorParts)> = Vec::new();
    for xd in xdxr_list {
        if xd.category != 1 {
            continue;
        }
        let date_key = xd.year * 10000 + xd.month * 100 + xd.day;
        events.push((date_key, FactorParts {
            div_per_share: xd.fenhong.unwrap_or(0.0) / 10.0,
            bonus_ratio: xd.songzhuangu.unwrap_or(0.0) / 10.0,
            rights_ratio: xd.peigu.unwrap_or(0.0) / 10.0,
            rights_price: xd.peigujia.unwrap_or(0.0),
        }));
    }
    events.sort_by_key(|e| e.0);

    // 2. 计算每个事件的因子
    let mut cumulative_qfq = 1.0;
    for &(date_key, parts) in &events {
        if let Some(p_close) = find_close_before_event(bars, context_bars, date_key) {
            let qfq_factor = calc_qfq_factor(p_close, &parts);
            let hfq_factor = if qfq_factor.abs() > 1e-10 { 1.0 / qfq_factor } else { 1.0 };

            cumulative_qfq *= qfq_factor;

            factors.push(FqFactorItem {
                date: date_key,
                close_before: p_close,
                qfq_factor,
                hfq_factor,
                div_per_share: parts.div_per_share,
                bonus_ratio: parts.bonus_ratio,
                rights_ratio: parts.rights_ratio,
                rights_price: parts.rights_price,
            });
        }
    }

    FqFactorResult {
        factors,
        cumulative_qfq,
        cumulative_hfq: if cumulative_qfq.abs() > 1e-10 { 1.0 / cumulative_qfq } else { 1.0 },
    }
}

/// 复权类型
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum FqType {
    None = 0,
    Qfq = 1,  // 前复权
    Hfq = 2,  // 后复权
}

/// 因子组成部分 (除权日价格无关的部分)
#[derive(Debug, Clone, Copy)]
struct FactorParts {
    div_per_share: f64,
    bonus_ratio: f64,
    rights_ratio: f64,
    rights_price: f64,
}

/// 计算实际前复权因子
///
/// factor = (P_close - D + P_rights × R_rights) / (P_close × (1 + R_bonus + R_rights))
fn calc_qfq_factor(close_before: f64, parts: &FactorParts) -> f64 {
    let denominator = close_before * (1.0 + parts.bonus_ratio + parts.rights_ratio);
    let numerator = close_before - parts.div_per_share + parts.rights_price * parts.rights_ratio;

    if denominator.abs() < 1e-10 || close_before.abs() < 1e-10 {
        return 1.0;
    }
    numerator / denominator
}

/// 将价格四舍五入到指定小数位数 (不影响 f64 内部精度, 仅消除浮点尾噪声)
/// scale 由调用方预先计算 (10^places), 避免循环内重复 powi
#[inline]
fn round_with_scale(p: f64, scale: f64) -> f64 {
    (p * scale).round() / scale
}

/// 查找事件前一交易日的收盘价
///
/// 先在 `bars` (主数据) 中搜索, 取最后一个日期 < date_key 的 bar；
/// 若未找到, 再在 `context_bars` (更早的历史 K 线) 中反向搜索。
/// bars 按日期升序 (调用契约), 用 partition_point 二分, O(log B)。
fn find_close_before_event(
    bars: &[SecurityBar],
    context_bars: &[SecurityBar],
    date_key: u32,
) -> Option<f64> {
    #[inline]
    fn bar_key(b: &SecurityBar) -> u32 {
        b.year as u32 * 10000 + b.month as u32 * 100 + b.day as u32
    }
    // bars 中找 (二分: 第一个 >= date_key 的位置, 其前一个即最后一个 < date_key 的)
    let pos = bars.partition_point(|b| bar_key(b) < date_key);
    if pos > 0 {
        return Some(bars[pos - 1].close);
    }
    // context_bars 中找 (反向迭代, 找最后一个日期 < date_key 的)
    context_bars
        .iter()
        .rev()
        .find(|b| bar_key(b) < date_key)
        .map(|b| b.close)
}

/// 对个股 K 线数据执行复权调整 (原地修改)
///
/// `bars`: 未复权 K 线数据 (按日期升序)，将被调整
/// `context_bars`: 额外的历史 K 线数据 (按日期升序)，仅用于因子计算，不会被修改
/// `xdxr_list`: 该股票的除权除息历史 (由 get_xdxr_info 获取)
/// `fq_type`: 复权类型
///
/// 精度: 内部全程 f64 计算, 最终输出时保留 3 位小数 (避免浮点尾噪声,
/// 不影响累乘精度)。成交量不做调整。
///
/// ## 历史上下文
///
/// 当 `context_bars` 非空时, 调整器先在其中搜索每个除权日的前收盘价,
/// 再在 `bars` 中搜索。这解决了早期除权事件位于请求 K 线数据范围之外
/// 导致因子被静默丢弃的问题 (例如 300750 的 800 根日 K 线仅覆盖
/// 2023-01 至 2026-05, 而 2019–2022 年有 4 个除权除息事件)。
pub fn adjust_security_bars(
    bars: &mut [SecurityBar],
    context_bars: &[SecurityBar],
    xdxr_list: &[XdXrInfo],
    fq_type: FqType,
) {
    if fq_type == FqType::None || bars.is_empty() || xdxr_list.is_empty() {
        return;
    }

    // 1. 构建除权事件 (升序)
    let mut events: Vec<(u32, FactorParts)> = Vec::new();
    for xd in xdxr_list {
        if xd.category != 1 {
            continue;
        }
        let date_key = xd.year * 10000 + xd.month * 100 + xd.day;
        events.push((date_key, FactorParts {
            div_per_share: xd.fenhong.unwrap_or(0.0) / 10.0,
            bonus_ratio: xd.songzhuangu.unwrap_or(0.0) / 10.0,
            rights_ratio: xd.peigu.unwrap_or(0.0) / 10.0,
            rights_price: xd.peigujia.unwrap_or(0.0),
        }));
    }
    events.sort_by_key(|e| e.0);

    // 2. 构建因子查找表: date_key → 前复权因子
    //    从 context_bars → bars 中找到每个除权日的前收盘价
    let mut factor_map: BTreeMap<u32, f64> = BTreeMap::new();
    for &(date_key, parts) in &events {
        if let Some(p_close) = find_close_before_event(bars, context_bars, date_key) {
            let factor = calc_qfq_factor(p_close, &parts);
            factor_map.insert(date_key, factor);
        }
    }

    if factor_map.is_empty() {
        return;
    }

    match fq_type {
        FqType::Qfq => {
            let mut cumulative = 1.0;
            let mut event_iter = events.iter().rev().peekable();
            for bar in bars.iter_mut().rev() {
                let bar_key = bar.year * 10000 + bar.month * 100 + bar.day;
                while let Some(&(evt_date, _parts)) = event_iter.peek() {
                    if *evt_date > bar_key {
                        if let Some(&factor) = factor_map.get(evt_date) {
                            cumulative *= factor;
                        }
                        event_iter.next();
                    } else {
                        break;
                    }
                }
                if (cumulative - 1.0).abs() > 1e-10 {
                    bar.open *= cumulative;
                    bar.high *= cumulative;
                    bar.low *= cumulative;
                    bar.close *= cumulative;
                }
            }
        }
        FqType::Hfq => {
            let mut cumulative = 1.0;
            let mut event_iter = events.iter().peekable();
            for bar in bars.iter_mut() {
                let bar_key = bar.year * 10000 + bar.month * 100 + bar.day;
                while let Some(&(evt_date, _)) = event_iter.peek() {
                    if *evt_date <= bar_key {
                        if let Some(&factor) = factor_map.get(evt_date) {
                            cumulative *= 1.0 / factor;
                        }
                        event_iter.next();
                    } else {
                        break;
                    }
                }
                if (cumulative - 1.0).abs() > 1e-10 {
                    bar.open *= cumulative;
                    bar.high *= cumulative;
                    bar.low *= cumulative;
                    bar.close *= cumulative;
                }
            }
        }
        FqType::None => {}
    }

    // 4. 最终精度控制: 消除浮点尾噪声 (scale 外提, 避免每 bar 重复 powi)
    let scale = 10_f64.powi(FQ_PRICE_PRECISION as i32);
    for bar in bars.iter_mut() {
        bar.open = round_with_scale(bar.open, scale);
        bar.high = round_with_scale(bar.high, scale);
        bar.low = round_with_scale(bar.low, scale);
        bar.close = round_with_scale(bar.close, scale);
    }
}

/// 对指数 K 线执行复权 (指数通常不需要复权, 保留接口一致性)
pub fn adjust_index_bars(bars: &mut [IndexBar], _xdxr_list: &[XdXrInfo], _fq_type: FqType) {
    // 指数不存在复权概念
    let _ = (bars, _xdxr_list, _fq_type);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_calc_qfq_factor_cash_div() {
        // 简单分红: 前收盘 46.90, 分红 1.0/股
        let parts = FactorParts { div_per_share: 1.0, bonus_ratio: 0.0, rights_ratio: 0.0, rights_price: 0.0 };
        let factor = calc_qfq_factor(46.90, &parts);
        let expected = (46.90 - 1.0) / 46.90;
        assert!((factor - expected).abs() < 1e-10);
    }

    #[test]
    fn test_calc_qfq_factor_bonus() {
        // 10送10: songzhuangu=10.0(每10股送10股)
        let parts = FactorParts { div_per_share: 0.0, bonus_ratio: 1.0, rights_ratio: 0.0, rights_price: 0.0 };
        let factor = calc_qfq_factor(20.0, &parts);
        // factor = 20 / (20 * 2) = 0.5
        assert!((factor - 0.5).abs() < 1e-10);
    }

    #[test]
    fn test_adjust_no_event() {
        let mut bars = vec![SecurityBar {
            open: 10.0, close: 11.0, high: 12.0, low: 9.0,
            vol: 100.0, amount: 1000.0,
            year: 2025, month: 6, day: 15, hour: 0, minute: 0,
            datetime: "2025-06-15".into(),
        }];
        let orig = bars[0].open;
        adjust_security_bars(&mut bars, &[], &[], FqType::Qfq);
        assert!((bars[0].open - orig).abs() < 1e-10);
    }

    #[test]
    fn test_find_close_before_fallback_to_context() {
        // bars 中无日期早于事件的数据, 应回退到 context
        let bars = vec![SecurityBar {
            open: 20.0, close: 21.0, high: 22.0, low: 19.0,
            vol: 100.0, amount: 1000.0,
            year: 2025, month: 6, day: 15, hour: 0, minute: 0,
            datetime: "".into(),
        }];
        let context = vec![SecurityBar {
            open: 10.0, close: 11.0, high: 12.0, low: 9.0,
            vol: 100.0, amount: 1000.0,
            year: 2024, month: 6, day: 15, hour: 0, minute: 0,
            datetime: "".into(),
        }];
        // event at 2025-01-01 — bars 中所有数据 > 2025-01-01, 应回退到 context
        let result = find_close_before_event(&bars, &context, 20250101);
        assert!(result.is_some());
        assert!((result.unwrap() - 11.0).abs() < 1e-10);
    }

    #[test]
    fn test_adjust_context_hfq() {
        // 后复权场景: events 在 bars 之前, close_before 从 context 获取
        let context = vec![SecurityBar {
            open: 100.0, close: 100.0, high: 101.0, low: 99.0,
            vol: 1000.0, amount: 100000.0,
            year: 2024, month: 5, day: 1, hour: 0, minute: 0,
            datetime: "".into(),
        }];
        let mut bars = vec![
            SecurityBar {
                open: 103.0, close: 105.0, high: 106.0, low: 102.0,
                vol: 1000.0, amount: 100000.0,
                year: 2024, month: 9, day: 1, hour: 0, minute: 0,
                datetime: "".into(),
            },
            SecurityBar {
                open: 110.0, close: 112.0, high: 113.0, low: 109.0,
                vol: 1000.0, amount: 100000.0,
                year: 2025, month: 3, day: 1, hour: 0, minute: 0,
                datetime: "".into(),
            },
        ];
        // Event at 2024-06-15: fenhong=5.0 元/10股 = 0.5 元/股
        // close_before from context: 2024-05-01 close=100.0
        let xdxr = vec![XdXrInfo {
            category: 1,
            year: 2024, month: 6, day: 15,
            name: String::new(),
            fenhong: Some(5.0), songzhuangu: Some(0.0),
            peigu: Some(0.0), peigujia: Some(0.0), suogu: Some(0.0),
            panqianliutong: None, panhouliutong: None,
            qianzongguben: None, houzongguben: None,
            fenshu: None, xingquanjia: None,
        }];
        // factor = (100.0 - 0.5) / 100.0 = 0.995
        // HFQ: bars after the event get cum *= 1/factor = 1.005025...
        // Both bars are after the event, so both get adjusted
        let expected_cum = 1.0 / 0.995;
        adjust_security_bars(&mut bars, &context, &xdxr, FqType::Hfq);
        assert!((bars[0].close - 105.0 * expected_cum).abs() < 0.01,
            "expected {}, got {}", 105.0 * expected_cum, bars[0].close);
        assert!((bars[1].close - 112.0 * expected_cum).abs() < 0.01,
            "expected {}, got {}", 112.0 * expected_cum, bars[1].close);
    }
}
