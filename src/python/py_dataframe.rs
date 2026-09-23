//! DataFrame 输出 — numpy 列式直建 (阶段 4 / B4)
//!
//! 数值列 PyArray1::from_vec 直建 (float64/int64, 每列一次分配, 零 PyObject 装箱),
//! 字符串列单次 PyList 构造; pandas 对 numpy 列零拷贝采纳。
//! 相比旧 dict-of-lists 每元素 into_py_any: 消除每 bar 10-14 次 PyObject 创建。
//!
//! dtype 语义 (相对 dict-of-lists 路径的行为改善, 见 CHANGELOG):
//!   价格/成交量/金额 -> float64; 年月日时分/市场/分类码 -> int64; 字符串 -> object

use numpy::PyArray1;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::protocol::types::*;

/// 组装 dict-of-arrays 并调用 pd.DataFrame()
/// (dict 保持插入序 -> 列名与列序与旧实现完全一致)
fn make_dataframe(py: Python<'_>, columns: Vec<(&str, Bound<'_, PyAny>)>) -> PyResult<Py<PyAny>> {
    let dict = PyDict::new(py);
    for (name, arr) in &columns {
        dict.set_item(*name, arr)?;
    }
    let pd = py.import("pandas")?;
    let df = pd.call_method1("DataFrame", (dict,))?;
    Ok(df.into())
}

// ---------- 列构造助手 ----------

/// f64 列 -> float64 ndarray
fn f64_col<'py>(py: Python<'py>, v: Vec<f64>) -> PyResult<Bound<'py, PyAny>> {
    Ok(PyArray1::from_vec(py, v).into_any())
}

/// u32 列 -> int64 ndarray
fn u32_col<'py>(py: Python<'py>, v: Vec<u32>) -> PyResult<Bound<'py, PyAny>> {
    Ok(PyArray1::from_vec(py, v.into_iter().map(|x| x as i64).collect()).into_any())
}

/// u8 列 -> int64 ndarray
fn u8_col<'py>(py: Python<'py>, v: Vec<u8>) -> PyResult<Bound<'py, PyAny>> {
    Ok(PyArray1::from_vec(py, v.into_iter().map(|x| x as i64).collect()).into_any())
}

/// u16 列 -> int64 ndarray
fn u16_col<'py>(py: Python<'py>, v: Vec<u16>) -> PyResult<Bound<'py, PyAny>> {
    Ok(PyArray1::from_vec(py, v.into_iter().map(|x| x as i64).collect()).into_any())
}

/// 字符串列 -> 单次 PyList (object)
fn str_col<'py>(py: Python<'py>, items: Vec<&str>) -> PyResult<Bound<'py, PyAny>> {
    Ok(PyList::new(py, items)?.into_any())
}

// ============================================================
// Bars DataFrame
// ============================================================

pub fn security_bars_to_df(py: Python<'_>, bars: &[SecurityBar]) -> PyResult<Py<PyAny>> {
    let n = bars.len();
    let (mut opens, mut highs, mut lows, mut closes) = (
        Vec::with_capacity(n), Vec::with_capacity(n),
        Vec::with_capacity(n), Vec::with_capacity(n),
    );
    let (mut vols, mut amounts) = (Vec::with_capacity(n), Vec::with_capacity(n));
    let (mut years, mut months, mut days, mut hours, mut minutes) = (
        Vec::with_capacity(n), Vec::with_capacity(n), Vec::with_capacity(n),
        Vec::with_capacity(n), Vec::with_capacity(n),
    );
    let mut datetimes = Vec::with_capacity(n);

    for b in bars {
        opens.push(b.open);
        highs.push(b.high);
        lows.push(b.low);
        closes.push(b.close);
        vols.push(b.vol);
        amounts.push(b.amount);
        years.push(b.year);
        months.push(b.month);
        days.push(b.day);
        hours.push(b.hour);
        minutes.push(b.minute);
        datetimes.push(b.datetime.as_str());
    }

    make_dataframe(py, vec![
        ("datetime", str_col(py, datetimes)?),
        ("year", u32_col(py, years)?),
        ("month", u32_col(py, months)?),
        ("day", u32_col(py, days)?),
        ("hour", u32_col(py, hours)?),
        ("minute", u32_col(py, minutes)?),
        ("open", f64_col(py, opens)?),
        ("high", f64_col(py, highs)?),
        ("low", f64_col(py, lows)?),
        ("close", f64_col(py, closes)?),
        ("vol", f64_col(py, vols)?),
        ("amount", f64_col(py, amounts)?),
    ])
}

pub fn index_bars_to_df(py: Python<'_>, bars: &[IndexBar]) -> PyResult<Py<PyAny>> {
    let n = bars.len();
    let (mut opens, mut highs, mut lows, mut closes) = (
        Vec::with_capacity(n), Vec::with_capacity(n),
        Vec::with_capacity(n), Vec::with_capacity(n),
    );
    let (mut vols, mut amounts) = (Vec::with_capacity(n), Vec::with_capacity(n));
    let (mut years, mut months, mut days, mut hours, mut minutes) = (
        Vec::with_capacity(n), Vec::with_capacity(n), Vec::with_capacity(n),
        Vec::with_capacity(n), Vec::with_capacity(n),
    );
    let (mut up_counts, mut down_counts) = (Vec::with_capacity(n), Vec::with_capacity(n));
    let mut datetimes = Vec::with_capacity(n);

    for b in bars {
        opens.push(b.open);
        highs.push(b.high);
        lows.push(b.low);
        closes.push(b.close);
        vols.push(b.vol);
        amounts.push(b.amount);
        years.push(b.year);
        months.push(b.month);
        days.push(b.day);
        hours.push(b.hour);
        minutes.push(b.minute);
        datetimes.push(b.datetime.as_str());
        up_counts.push(b.up_count);
        down_counts.push(b.down_count);
    }

    make_dataframe(py, vec![
        ("datetime", str_col(py, datetimes)?),
        ("year", u32_col(py, years)?),
        ("month", u32_col(py, months)?),
        ("day", u32_col(py, days)?),
        ("hour", u32_col(py, hours)?),
        ("minute", u32_col(py, minutes)?),
        ("open", f64_col(py, opens)?),
        ("high", f64_col(py, highs)?),
        ("low", f64_col(py, lows)?),
        ("close", f64_col(py, closes)?),
        ("vol", f64_col(py, vols)?),
        ("amount", f64_col(py, amounts)?),
        ("up_count", u32_col(py, up_counts)?),
        ("down_count", u32_col(py, down_counts)?),
    ])
}

// ============================================================
// Quotes DataFrame
// ============================================================

pub fn quotes_to_df(py: Python<'_>, quotes: &[SecurityQuote]) -> PyResult<Py<PyAny>> {
    let n = quotes.len();
    let mut markets = Vec::with_capacity(n);
    let (mut prices, mut last_close) = (Vec::with_capacity(n), Vec::with_capacity(n));
    let (mut opens, mut highs, mut lows) = (
        Vec::with_capacity(n), Vec::with_capacity(n), Vec::with_capacity(n),
    );
    let (mut vols, mut cur_vols, mut amounts) = (
        Vec::with_capacity(n), Vec::with_capacity(n), Vec::with_capacity(n),
    );
    let (mut s_vols, mut b_vols) = (Vec::with_capacity(n), Vec::with_capacity(n));
    let (mut codes, mut servers) = (Vec::with_capacity(n), Vec::with_capacity(n));

    for q in quotes {
        markets.push(q.market);
        codes.push(q.code.as_str());
        prices.push(q.price);
        last_close.push(q.last_close);
        opens.push(q.open);
        highs.push(q.high);
        lows.push(q.low);
        vols.push(q.vol);
        cur_vols.push(q.cur_vol);
        amounts.push(q.amount);
        s_vols.push(q.s_vol);
        b_vols.push(q.b_vol);
        servers.push(q.servertime.as_str());
    }

    make_dataframe(py, vec![
        ("code", str_col(py, codes)?),
        ("market", u8_col(py, markets)?),
        ("price", f64_col(py, prices)?),
        ("last_close", f64_col(py, last_close)?),
        ("open", f64_col(py, opens)?),
        ("high", f64_col(py, highs)?),
        ("low", f64_col(py, lows)?),
        ("vol", f64_col(py, vols)?),
        ("cur_vol", f64_col(py, cur_vols)?),
        ("amount", f64_col(py, amounts)?),
        ("s_vol", f64_col(py, s_vols)?),
        ("b_vol", f64_col(py, b_vols)?),
        ("servertime", str_col(py, servers)?),
    ])
}

// ============================================================
// DailyBarRecord DataFrame (Reader)
// ============================================================

pub fn daily_records_to_df(
    py: Python<'_>,
    records: &[crate::reader::daily_bar::DailyBarRecord],
) -> PyResult<Py<PyAny>> {
    let n = records.len();
    let (mut opens, mut highs, mut lows, mut closes) = (
        Vec::with_capacity(n), Vec::with_capacity(n),
        Vec::with_capacity(n), Vec::with_capacity(n),
    );
    let (mut amounts, mut volumes) = (Vec::with_capacity(n), Vec::with_capacity(n));
    let (mut years, mut months, mut days_v) = (
        Vec::with_capacity(n), Vec::with_capacity(n), Vec::with_capacity(n),
    );
    let mut dates = Vec::with_capacity(n);

    for r in records {
        dates.push(r.date.as_str());
        opens.push(r.open);
        highs.push(r.high);
        lows.push(r.low);
        closes.push(r.close);
        amounts.push(r.amount);
        volumes.push(r.volume);
        years.push(r.year);
        months.push(r.month);
        days_v.push(r.day);
    }

    make_dataframe(py, vec![
        ("date", str_col(py, dates)?),
        ("year", u32_col(py, years)?),
        ("month", u32_col(py, months)?),
        ("day", u32_col(py, days_v)?),
        ("open", f64_col(py, opens)?),
        ("high", f64_col(py, highs)?),
        ("low", f64_col(py, lows)?),
        ("close", f64_col(py, closes)?),
        ("volume", f64_col(py, volumes)?),
        ("amount", f64_col(py, amounts)?),
    ])
}

// ============================================================
// Finance DataFrame (multi-stock)
// ============================================================

pub fn finance_to_df(py: Python<'_>, infos: &[(FinanceInfo,)]) -> PyResult<Py<PyAny>> {
    let n = infos.len();
    let mut markets = Vec::with_capacity(n);
    let (mut zonggubens, mut liutonggubens) = (Vec::with_capacity(n), Vec::with_capacity(n));
    let (mut jingzichans, mut jingliruns) = (Vec::with_capacity(n), Vec::with_capacity(n));
    let (mut zhuyingshourus, mut meigujingzichans) = (Vec::with_capacity(n), Vec::with_capacity(n));
    let mut yingyeliruns = Vec::with_capacity(n);
    let (mut provinces, mut industries) = (Vec::with_capacity(n), Vec::with_capacity(n));
    let mut codes = Vec::with_capacity(n);

    for (info,) in infos {
        markets.push(info.market);
        codes.push(info.code.as_str());
        zonggubens.push(info.zongguben);
        liutonggubens.push(info.liutongguben);
        jingzichans.push(info.jingzichan);
        jingliruns.push(info.jinglirun);
        zhuyingshourus.push(info.zhuyingshouru);
        meigujingzichans.push(info.meigujingzichan);
        yingyeliruns.push(info.yingyelirun);
        provinces.push(info.province);
        industries.push(info.industry);
    }

    make_dataframe(py, vec![
        ("code", str_col(py, codes)?),
        ("market", u8_col(py, markets)?),
        ("zongguben", f64_col(py, zonggubens)?),
        ("liutongguben", f64_col(py, liutonggubens)?),
        ("jingzichan", f64_col(py, jingzichans)?),
        ("jinglirun", f64_col(py, jingliruns)?),
        ("zhuyingshouru", f64_col(py, zhuyingshourus)?),
        ("yingyelirun", f64_col(py, yingyeliruns)?),
        ("meigujingzichan", f64_col(py, meigujingzichans)?),
        ("province", u16_col(py, provinces)?),
        ("industry", u16_col(py, industries)?),
    ])
}

// ============================================================
// MinBarRecord DataFrame
// ============================================================

pub fn min_records_to_df(
    py: Python<'_>,
    records: &[crate::reader::min_bar::MinBarRecord],
) -> PyResult<Py<PyAny>> {
    let n = records.len();
    let (mut opens, mut highs, mut lows, mut closes) = (
        Vec::with_capacity(n), Vec::with_capacity(n),
        Vec::with_capacity(n), Vec::with_capacity(n),
    );
    let (mut amounts, mut volumes) = (Vec::with_capacity(n), Vec::with_capacity(n));
    let (mut years, mut months, mut days_v, mut hours, mut minutes) = (
        Vec::with_capacity(n), Vec::with_capacity(n), Vec::with_capacity(n),
        Vec::with_capacity(n), Vec::with_capacity(n),
    );
    let mut dates = Vec::with_capacity(n);

    for r in records {
        dates.push(r.date.as_str());
        opens.push(r.open);
        highs.push(r.high);
        lows.push(r.low);
        closes.push(r.close);
        amounts.push(r.amount);
        volumes.push(r.volume);
        years.push(r.year);
        months.push(r.month);
        days_v.push(r.day);
        hours.push(r.hour);
        minutes.push(r.minute);
    }

    make_dataframe(py, vec![
        ("date", str_col(py, dates)?),
        ("year", u32_col(py, years)?),
        ("month", u32_col(py, months)?),
        ("day", u32_col(py, days_v)?),
        ("hour", u32_col(py, hours)?),
        ("minute", u32_col(py, minutes)?),
        ("open", f64_col(py, opens)?),
        ("high", f64_col(py, highs)?),
        ("low", f64_col(py, lows)?),
        ("close", f64_col(py, closes)?),
        ("volume", f64_col(py, volumes)?),
        ("amount", f64_col(py, amounts)?),
    ])
}
