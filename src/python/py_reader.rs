use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use pyo3::IntoPyObjectExt;

use crate::reader::block;
use crate::reader::daily_bar;
use crate::reader::financial;
use crate::reader::min_bar;

/// 日线 Reader - 对应 Python tdxpy 的 TdxDailyBarReader
#[pyclass]
pub struct DailyBarReader {
    coefficient: f64,
}

#[pymethods]
impl DailyBarReader {
    #[new]
    #[pyo3(signature = (coefficient=0.01))]
    fn new(coefficient: f64) -> Self {
        Self { coefficient }
    }

    /// 解析日线数据，返回 Python list of dict
    fn parse_data(&self, py: Python<'_>, data: Vec<u8>) -> PyResult<Py<PyAny>> {
        let records = daily_bar::parse_daily_bar(&data, self.coefficient)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

        let list = PyList::empty(py);
        for r in &records {
            let dict = PyDict::new(py);
            dict.set_item("date", &r.date)?;
            dict.set_item("open", r.open)?;
            dict.set_item("high", r.high)?;
            dict.set_item("low", r.low)?;
            dict.set_item("close", r.close)?;
            dict.set_item("amount", r.amount)?;
            dict.set_item("volume", r.volume)?;
            dict.set_item("year", r.year)?;
            dict.set_item("month", r.month)?;
            dict.set_item("day", r.day)?;
            list.append(dict)?;
        }
        Ok(list.into())
    }

    /// 从文件读取并解析
    fn parse_file(&self, py: Python<'_>, filename: &str) -> PyResult<Py<PyAny>> {
        let records = daily_bar::read_daily_bar_file(filename, self.coefficient)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

        let list = PyList::empty(py);
        for r in &records {
            let dict = PyDict::new(py);
            dict.set_item("date", &r.date)?;
            dict.set_item("open", r.open)?;
            dict.set_item("high", r.high)?;
            dict.set_item("low", r.low)?;
            dict.set_item("close", r.close)?;
            dict.set_item("amount", r.amount)?;
            dict.set_item("volume", r.volume)?;
            dict.set_item("year", r.year)?;
            dict.set_item("month", r.month)?;
            dict.set_item("day", r.day)?;
            list.append(dict)?;
        }
        Ok(list.into())
    }

    /// 解析日线数据，返回 Python list of tuple (高性能模式)
    fn parse_data_tuples(&self, py: Python<'_>, data: Vec<u8>) -> PyResult<Py<PyAny>> {
        let records = daily_bar::parse_daily_bar(&data, self.coefficient)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

        let list = PyList::empty(py);
        for r in &records {
            let date = r.date.as_str().into_py_any(py)?;
            let open = r.open.into_py_any(py)?;
            let high = r.high.into_py_any(py)?;
            let low = r.low.into_py_any(py)?;
            let close = r.close.into_py_any(py)?;
            let amount = r.amount.into_py_any(py)?;
            let volume = r.volume.into_py_any(py)?;
            let year = r.year.into_py_any(py)?;
            let month = r.month.into_py_any(py)?;
            let day = r.day.into_py_any(py)?;
            let tuple = PyTuple::new(py, [date, open, high, low, close, amount, volume, year, month, day])?;
            list.append(tuple)?;
        }
        Ok(list.into())
    }

    /// 从文件读取并解析，返回 Python list of tuple (高性能模式)
    fn parse_file_tuples(&self, py: Python<'_>, filename: &str) -> PyResult<Py<PyAny>> {
        let records = daily_bar::read_daily_bar_file(filename, self.coefficient)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

        let list = PyList::empty(py);
        for r in &records {
            let date = r.date.as_str().into_py_any(py)?;
            let open = r.open.into_py_any(py)?;
            let high = r.high.into_py_any(py)?;
            let low = r.low.into_py_any(py)?;
            let close = r.close.into_py_any(py)?;
            let amount = r.amount.into_py_any(py)?;
            let volume = r.volume.into_py_any(py)?;
            let year = r.year.into_py_any(py)?;
            let month = r.month.into_py_any(py)?;
            let day = r.day.into_py_any(py)?;
            let tuple = PyTuple::new(py, [date, open, high, low, close, amount, volume, year, month, day])?;
            list.append(tuple)?;
        }
        Ok(list.into())
    }

    /// 解析日线数据, 返回 pandas DataFrame (列式, 高性能)
    fn to_dataframe(&self, py: Python<'_>, data: Vec<u8>) -> PyResult<Py<PyAny>> {
        let records = daily_bar::parse_daily_bar(&data, self.coefficient)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
        crate::python::py_dataframe::daily_records_to_df(py, &records)
    }

    /// 从文件读取并解析, 返回 pandas DataFrame
    fn to_dataframe_file(&self, py: Python<'_>, filename: &str) -> PyResult<Py<PyAny>> {
        let records = daily_bar::read_daily_bar_file(filename, self.coefficient)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
        crate::python::py_dataframe::daily_records_to_df(py, &records)
    }
}

/// 5分钟线 Reader - 对应 Python tdxpy 的 TdxMinBarReader
#[pyclass]
pub struct MinBarReader;

#[pymethods]
impl MinBarReader {
    #[new]
    fn new() -> Self {
        Self
    }

    /// 解析 5分钟线数据 (整数格式)
    fn parse_data(&self, py: Python<'_>, data: Vec<u8>) -> PyResult<Py<PyAny>> {
        let records = min_bar::parse_min_bar(&data)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

        let list = PyList::empty(py);
        for r in &records {
            let dict = PyDict::new(py);
            dict.set_item("date", &r.date)?;
            dict.set_item("open", r.open)?;
            dict.set_item("high", r.high)?;
            dict.set_item("low", r.low)?;
            dict.set_item("close", r.close)?;
            dict.set_item("amount", r.amount)?;
            dict.set_item("volume", r.volume)?;
            dict.set_item("year", r.year)?;
            dict.set_item("month", r.month)?;
            dict.set_item("day", r.day)?;
            dict.set_item("hour", r.hour)?;
            dict.set_item("minute", r.minute)?;
            list.append(dict)?;
        }
        Ok(list.into())
    }

    /// 从文件读取并解析
    fn parse_file(&self, py: Python<'_>, filename: &str) -> PyResult<Py<PyAny>> {
        let records = min_bar::read_min_bar_file(filename)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

        let list = PyList::empty(py);
        for r in &records {
            let dict = PyDict::new(py);
            dict.set_item("date", &r.date)?;
            dict.set_item("open", r.open)?;
            dict.set_item("high", r.high)?;
            dict.set_item("low", r.low)?;
            dict.set_item("close", r.close)?;
            dict.set_item("amount", r.amount)?;
            dict.set_item("volume", r.volume)?;
            dict.set_item("year", r.year)?;
            dict.set_item("month", r.month)?;
            dict.set_item("day", r.day)?;
            dict.set_item("hour", r.hour)?;
            dict.set_item("minute", r.minute)?;
            list.append(dict)?;
        }
        Ok(list.into())
    }

    /// 解析 5分钟线数据，返回 list of tuple (高性能模式)
    fn parse_data_tuples(&self, py: Python<'_>, data: Vec<u8>) -> PyResult<Py<PyAny>> {
        let records = min_bar::parse_min_bar(&data)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

        let list = PyList::empty(py);
        for r in &records {
            let date = r.date.as_str().into_py_any(py)?;
            let open = r.open.into_py_any(py)?;
            let high = r.high.into_py_any(py)?;
            let low = r.low.into_py_any(py)?;
            let close = r.close.into_py_any(py)?;
            let amount = r.amount.into_py_any(py)?;
            let volume = r.volume.into_py_any(py)?;
            let year = r.year.into_py_any(py)?;
            let month = r.month.into_py_any(py)?;
            let day = r.day.into_py_any(py)?;
            let hour = r.hour.into_py_any(py)?;
            let minute = r.minute.into_py_any(py)?;
            let tuple = PyTuple::new(py, [date, open, high, low, close, amount, volume, year, month, day, hour, minute])?;
            list.append(tuple)?;
        }
        Ok(list.into())
    }

    /// 从文件读取并解析，返回 list of tuple (高性能模式)
    fn parse_file_tuples(&self, py: Python<'_>, filename: &str) -> PyResult<Py<PyAny>> {
        let records = min_bar::read_min_bar_file(filename)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

        let list = PyList::empty(py);
        for r in &records {
            let date = r.date.as_str().into_py_any(py)?;
            let open = r.open.into_py_any(py)?;
            let high = r.high.into_py_any(py)?;
            let low = r.low.into_py_any(py)?;
            let close = r.close.into_py_any(py)?;
            let amount = r.amount.into_py_any(py)?;
            let volume = r.volume.into_py_any(py)?;
            let year = r.year.into_py_any(py)?;
            let month = r.month.into_py_any(py)?;
            let day = r.day.into_py_any(py)?;
            let hour = r.hour.into_py_any(py)?;
            let minute = r.minute.into_py_any(py)?;
            let tuple = PyTuple::new(py, [date, open, high, low, close, amount, volume, year, month, day, hour, minute])?;
            list.append(tuple)?;
        }
        Ok(list.into())
    }

    /// 解析5分钟线数据, 返回 pandas DataFrame
    fn to_dataframe(&self, py: Python<'_>, data: Vec<u8>) -> PyResult<Py<PyAny>> {
        let records = min_bar::parse_min_bar(&data)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
        crate::python::py_dataframe::min_records_to_df(py, &records)
    }
}

/// LC 格式分钟线 Reader - 对应 Python tdxpy 的 TdxLCMinBarReader
#[pyclass]
pub struct LcMinBarReader;

#[pymethods]
impl LcMinBarReader {
    #[new]
    fn new() -> Self {
        Self
    }

    /// 解析 LC 格式分钟线数据 (浮点格式)
    fn parse_data(&self, py: Python<'_>, data: Vec<u8>) -> PyResult<Py<PyAny>> {
        let records = min_bar::parse_lc_min_bar(&data)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

        let list = PyList::empty(py);
        for r in &records {
            let dict = PyDict::new(py);
            dict.set_item("date", &r.date)?;
            dict.set_item("open", r.open)?;
            dict.set_item("high", r.high)?;
            dict.set_item("low", r.low)?;
            dict.set_item("close", r.close)?;
            dict.set_item("amount", r.amount)?;
            dict.set_item("volume", r.volume)?;
            dict.set_item("year", r.year)?;
            dict.set_item("month", r.month)?;
            dict.set_item("day", r.day)?;
            dict.set_item("hour", r.hour)?;
            dict.set_item("minute", r.minute)?;
            list.append(dict)?;
        }
        Ok(list.into())
    }

    /// 从文件读取并解析
    fn parse_file(&self, py: Python<'_>, filename: &str) -> PyResult<Py<PyAny>> {
        let records = min_bar::read_lc_min_bar_file(filename)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

        let list = PyList::empty(py);
        for r in &records {
            let dict = PyDict::new(py);
            dict.set_item("date", &r.date)?;
            dict.set_item("open", r.open)?;
            dict.set_item("high", r.high)?;
            dict.set_item("low", r.low)?;
            dict.set_item("close", r.close)?;
            dict.set_item("amount", r.amount)?;
            dict.set_item("volume", r.volume)?;
            dict.set_item("year", r.year)?;
            dict.set_item("month", r.month)?;
            dict.set_item("day", r.day)?;
            dict.set_item("hour", r.hour)?;
            dict.set_item("minute", r.minute)?;
            list.append(dict)?;
        }
        Ok(list.into())
    }

    /// 解析 LC 格式分钟线数据，返回 list of tuple (高性能模式)
    fn parse_data_tuples(&self, py: Python<'_>, data: Vec<u8>) -> PyResult<Py<PyAny>> {
        let records = min_bar::parse_lc_min_bar(&data)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

        let list = PyList::empty(py);
        for r in &records {
            let date = r.date.as_str().into_py_any(py)?;
            let open = r.open.into_py_any(py)?;
            let high = r.high.into_py_any(py)?;
            let low = r.low.into_py_any(py)?;
            let close = r.close.into_py_any(py)?;
            let amount = r.amount.into_py_any(py)?;
            let volume = r.volume.into_py_any(py)?;
            let year = r.year.into_py_any(py)?;
            let month = r.month.into_py_any(py)?;
            let day = r.day.into_py_any(py)?;
            let hour = r.hour.into_py_any(py)?;
            let minute = r.minute.into_py_any(py)?;
            let tuple = PyTuple::new(py, [date, open, high, low, close, amount, volume, year, month, day, hour, minute])?;
            list.append(tuple)?;
        }
        Ok(list.into())
    }

    /// 从文件读取并解析，返回 list of tuple (高性能模式)
    fn parse_file_tuples(&self, py: Python<'_>, filename: &str) -> PyResult<Py<PyAny>> {
        let records = min_bar::read_lc_min_bar_file(filename)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

        let list = PyList::empty(py);
        for r in &records {
            let date = r.date.as_str().into_py_any(py)?;
            let open = r.open.into_py_any(py)?;
            let high = r.high.into_py_any(py)?;
            let low = r.low.into_py_any(py)?;
            let close = r.close.into_py_any(py)?;
            let amount = r.amount.into_py_any(py)?;
            let volume = r.volume.into_py_any(py)?;
            let year = r.year.into_py_any(py)?;
            let month = r.month.into_py_any(py)?;
            let day = r.day.into_py_any(py)?;
            let hour = r.hour.into_py_any(py)?;
            let minute = r.minute.into_py_any(py)?;
            let tuple = PyTuple::new(py, [date, open, high, low, close, amount, volume, year, month, day, hour, minute])?;
            list.append(tuple)?;
        }
        Ok(list.into())
    }

    /// 解析LC分钟线数据, 返回 pandas DataFrame
    fn to_dataframe(&self, py: Python<'_>, data: Vec<u8>) -> PyResult<Py<PyAny>> {
        let records = min_bar::parse_lc_min_bar(&data)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
        let n = records.len();
        let mut dates: Vec<Py<PyAny>> = Vec::with_capacity(n);
        let mut opens: Vec<Py<PyAny>> = Vec::with_capacity(n);
        let mut highs: Vec<Py<PyAny>> = Vec::with_capacity(n);
        let mut lows: Vec<Py<PyAny>> = Vec::with_capacity(n);
        let mut closes: Vec<Py<PyAny>> = Vec::with_capacity(n);
        let mut amounts: Vec<Py<PyAny>> = Vec::with_capacity(n);
        let mut volumes: Vec<Py<PyAny>> = Vec::with_capacity(n);
        let mut years: Vec<Py<PyAny>> = Vec::with_capacity(n);
        let mut months: Vec<Py<PyAny>> = Vec::with_capacity(n);
        let mut days_v: Vec<Py<PyAny>> = Vec::with_capacity(n);
        let mut hours: Vec<Py<PyAny>> = Vec::with_capacity(n);
        let mut minutes: Vec<Py<PyAny>> = Vec::with_capacity(n);

        for r in &records {
            dates.push(r.date.as_str().into_py_any(py)?);
            opens.push(r.open.into_py_any(py)?);
            highs.push(r.high.into_py_any(py)?);
            lows.push(r.low.into_py_any(py)?);
            closes.push(r.close.into_py_any(py)?);
            amounts.push(r.amount.into_py_any(py)?);
            volumes.push(r.volume.into_py_any(py)?);
            years.push(r.year.into_py_any(py)?);
            months.push(r.month.into_py_any(py)?);
            days_v.push(r.day.into_py_any(py)?);
            hours.push(r.hour.into_py_any(py)?);
            minutes.push(r.minute.into_py_any(py)?);
        }

        let dict = PyDict::new(py);
        dict.set_item("date", dates.as_slice())?;
        dict.set_item("open", opens.as_slice())?;
        dict.set_item("high", highs.as_slice())?;
        dict.set_item("low", lows.as_slice())?;
        dict.set_item("close", closes.as_slice())?;
        dict.set_item("amount", amounts.as_slice())?;
        dict.set_item("volume", volumes.as_slice())?;
        dict.set_item("year", years.as_slice())?;
        dict.set_item("month", months.as_slice())?;
        dict.set_item("day", days_v.as_slice())?;
        dict.set_item("hour", hours.as_slice())?;
        dict.set_item("minute", minutes.as_slice())?;
        let pd = py.import("pandas")?;
        let df = pd.call_method1("DataFrame", (dict,))?;
        Ok(df.into())
    }
}

/// 板块文件 Reader
#[pyclass]
pub struct BlockReader;

#[pymethods]
impl BlockReader {
    #[new]
    fn new() -> Self {
        Self
    }

    /// 解析板块数据 (扁平模式: 每只股票一行)
    fn parse_data(&self, py: Python<'_>, data: Vec<u8>) -> PyResult<Py<PyAny>> {
        let records = block::parse_block(&data)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

        let list = PyList::empty(py);
        for r in &records {
            let dict = PyDict::new(py);
            dict.set_item("blockname", &r.blockname)?;
            dict.set_item("block_type", r.block_type)?;
            dict.set_item("code_index", r.code_index)?;
            dict.set_item("code", &r.code)?;
            list.append(dict)?;
        }
        Ok(list.into())
    }

    /// 解析板块数据 (分组模式: 每个板块一行)
    fn parse_data_group(&self, py: Python<'_>, data: Vec<u8>) -> PyResult<Py<PyAny>> {
        let groups = block::parse_block_group(&data)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

        let list = PyList::empty(py);
        for g in &groups {
            let dict = PyDict::new(py);
            dict.set_item("blockname", &g.blockname)?;
            dict.set_item("block_type", g.block_type)?;
            dict.set_item("stock_count", g.stock_count)?;
            dict.set_item("code_list", &g.code_list)?;
            list.append(dict)?;
        }
        Ok(list.into())
    }

    /// 从文件读取并解析
    fn parse_file(&self, py: Python<'_>, filename: &str) -> PyResult<Py<PyAny>> {
        let records = block::read_block_file(filename)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

        let list = PyList::empty(py);
        for r in &records {
            let dict = PyDict::new(py);
            dict.set_item("blockname", &r.blockname)?;
            dict.set_item("block_type", r.block_type)?;
            dict.set_item("code_index", r.code_index)?;
            dict.set_item("code", &r.code)?;
            list.append(dict)?;
        }
        Ok(list.into())
    }
}

/// 财务数据 Reader
#[pyclass]
pub struct FinancialReader;

#[pymethods]
impl FinancialReader {
    #[new]
    fn new() -> Self {
        Self
    }

    /// 解析财务数据
    fn parse_data(&self, py: Python<'_>, data: Vec<u8>) -> PyResult<Py<PyAny>> {
        let records = financial::parse_financial(&data)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

        let list = PyList::empty(py);
        for r in &records {
            let dict = PyDict::new(py);
            dict.set_item("code", &r.code)?;
            dict.set_item("report_date", r.report_date)?;
            let fields = PyList::empty(py);
            for f in &r.fields {
                fields.append(f)?;
            }
            dict.set_item("fields", fields)?;
            list.append(dict)?;
        }
        Ok(list.into())
    }

    /// 从文件读取并解析
    fn parse_file(&self, py: Python<'_>, filename: &str) -> PyResult<Py<PyAny>> {
        let records = financial::read_financial_file(filename)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

        let list = PyList::empty(py);
        for r in &records {
            let dict = PyDict::new(py);
            dict.set_item("code", &r.code)?;
            dict.set_item("report_date", r.report_date)?;
            let fields = PyList::empty(py);
            for f in &r.fields {
                fields.append(f)?;
            }
            dict.set_item("fields", fields)?;
            list.append(dict)?;
        }
        Ok(list.into())
    }
}
