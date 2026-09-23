/// F10 公司资料 Python 绑定

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::net::f10_client::TdxF10Client as RustTdxF10Client;
use crate::profile::{F10Category, auto_market, parse_f10_text, extract_basic_info};

/// F10 公司资料客户端 (Python 绑定)
///
/// 提供获取通达信 F10 公司基本面资料数据的能力。
/// 使用独立连接，不占用共享连接池。
///
/// # 示例
///
/// ```python
/// from tdxrs.pro import TdxF10Client
///
/// # 创建客户端并指定服务器
/// client = TdxF10Client("180.153.18.170", 7709)
///
/// # 获取分类列表
/// categories = client.get_category(1, "600519")
/// for cat in categories:
///     print(f"{cat['name']}: {cat['length']} bytes")
///
/// # 获取指定分类内容
/// content = client.get_content_by_name(1, "600519", "公司概况")
/// print(content[:500])
/// ```
#[pyclass(name = "TdxF10Client")]
pub struct PyTdxF10Client {
    client: RustTdxF10Client,
}

#[pymethods]
impl PyTdxF10Client {
    /// 创建新的 F10 客户端
    ///
    /// # 参数
    /// * `ip` - 服务器 IP 地址
    /// * `port` - 服务器端口 (默认 7709)
    /// * `timeout` - 超时时间 (秒，默认 10)
    #[new]
    #[pyo3(signature = (ip, port=7709, timeout=10.0))]
    fn new(ip: &str, port: u16, timeout: f64) -> Self {
        let client = RustTdxF10Client::new(ip, port, Some(timeout));
        Self { client }
    }

    /// 设置服务器地址
    fn set_server(&mut self, ip: &str, port: u16) {
        self.client.set_server(ip, port);
    }

    /// 设置超时时间
    fn set_timeout(&mut self, secs: f64) {
        self.client.set_timeout(secs);
    }

    /// 获取 F10 分类列表
    ///
    /// # 参数
    /// * `market` - 市场代码 (0=SZ, 1=SH)
    /// * `code` - 股票代码
    ///
    /// # 返回
    /// 分类列表，每个元素是字典:
    /// - name: 分类名称
    /// - filename: 文件名
    /// - start: 起始位置
    /// - length: 数据长度
    fn get_category(&self, py: Python<'_>, market: u8, code: &str) -> PyResult<Py<PyList>> {
        let categories = py.detach(|| {
            self.client.get_category(market, code).map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("获取分类失败: {}", e))
            })
        })?;

        let list = PyList::empty(py);
        for cat in &categories {
            let dict = PyDict::new(py);
            dict.set_item("name", &cat.name)?;
            dict.set_item("filename", &cat.filename)?;
            dict.set_item("start", cat.start)?;
            dict.set_item("length", cat.length)?;
            list.append(dict)?;
        }

        Ok(list.unbind())
    }

    /// 获取 F10 分类列表 (自动识别市场)
    ///
    /// # 参数
    /// * `code` - 股票代码
    ///
    /// # 返回
    /// 分类列表
    fn get_category_auto(&self, py: Python<'_>, code: &str) -> PyResult<Py<PyList>> {
        let market = auto_market(code).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("无法识别市场: {}", e))
        })?;
        self.get_category(py, market, code)
    }

    /// 获取 F10 内容
    ///
    /// # 参数
    /// * `market` - 市场代码
    /// * `code` - 股票代码
    /// * `category` - 分类字典 (从 get_category 获取)
    ///
    /// # 返回
    /// 文本内容
    fn get_content(
        &self,
        market: u8,
        code: &str,
        category: &Bound<'_, PyDict>,
    ) -> PyResult<String> {
        // 从字典构建 F10Category
        let name: String = category.get_item("name")?.ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err("缺少 'name' 字段")
        })?.extract()?;
        let filename: String = category.get_item("filename")?.ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err("缺少 'filename' 字段")
        })?.extract()?;
        let start: u32 = category.get_item("start")?.ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err("缺少 'start' 字段")
        })?.extract()?;
        let length: u32 = category.get_item("length")?.ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err("缺少 'length' 字段")
        })?.extract()?;

        let cat = F10Category::new(name, filename, start, length);

        let content = py.detach(|| {
            self.client.get_content(market, code, &cat).map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("获取内容失败: {}", e))
            })
        })?;

        Ok(content.content)
    }

    /// 获取指定分类的内容
    ///
    /// # 参数
    /// * `market` - 市场代码
    /// * `code` - 股票代码
    /// * `name` - 分类名称 (如 "公司概况")
    ///
    /// # 返回
    /// 文本内容
    fn get_content_by_name(&self, py: Python<'_>, market: u8, code: &str, name: &str) -> PyResult<String> {
        let content = py.detach(|| {
            self.client.get_content_by_name(market, code, name).map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("获取内容失败: {}", e))
            })
        })?;

        Ok(content.content)
    }

    /// 获取所有分类的内容
    ///
    /// # 参数
    /// * `market` - 市场代码
    /// * `code` - 股票代码
    ///
    /// # 返回
    /// 字典，键为分类名称，值为文本内容
    fn get_all_contents(&self, py: Python<'_>, market: u8, code: &str) -> PyResult<Py<PyDict>> {
        let contents = self.client.get_all_contents(market, code).map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("获取所有内容失败: {}", e))
        })?;

        let dict = PyDict::new(py);
        for content in &contents {
            dict.set_item(&content.category, &content.content)?;
        }

        Ok(dict.unbind())
    }

    /// 获取所有分类的内容 (返回 F10Data)
    ///
    /// # 参数
    /// * `market` - 市场代码
    /// * `code` - 股票代码
    ///
    /// # 返回
    /// F10Data 包含所有分类的内容
    fn get_all_data(&self, py: Python<'_>, market: u8, code: &str) -> PyResult<Py<PyDict>> {
        let data = py.detach(|| {
            self.client.get_all_data(market, code).map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("获取所有数据失败: {}", e))
            })
        })?;

        let dict = PyDict::new(py);
        dict.set_item("code", &data.code)?;
        dict.set_item("market", data.market)?;
        dict.set_item("category_count", data.category_count())?;
        dict.set_item("total_chars", data.total_chars())?;
        dict.set_item("total_bytes", data.total_bytes())?;

        let contents = PyList::empty(py);
        for content in &data.contents {
            let item = PyDict::new(py);
            item.set_item("category", &content.category)?;
            item.set_item("content", &content.content)?;
            item.set_item("byte_length", content.byte_length)?;
            contents.append(item)?;
        }
        dict.set_item("contents", contents)?;

        Ok(dict.unbind())
    }

    /// 验证股票代码格式
    #[staticmethod]
    fn is_valid_code(code: &str) -> bool {
        code.len() == 6 && code.chars().all(|c| c.is_ascii_digit())
    }

    /// 自动识别市场代码
    ///
    /// # 参数
    /// * `code` - 股票代码
    ///
    /// # 返回
    /// 市场代码 (0=SZ, 1=SH)
    #[staticmethod]
    fn auto_market_code(code: &str) -> PyResult<u8> {
        auto_market(code).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("无法识别市场: {}", e))
        })
    }

    /// 解析 F10 原始文本，返回结构化数据
    ///
    /// # 参数
    /// * `text` - F10 原始文本 (从 get_content 等方法获取)
    ///
    /// # 返回
    /// 字典，包含:
    /// - basic_info: 基本资料
    /// - listing_info: 发行上市信息
    /// - sections: 所有章节内容
    #[staticmethod]
    fn parse_f10(py: Python<'_>, text: &str) -> PyResult<Py<PyDict>> {
        let parsed = parse_f10_text(text);

        let dict = PyDict::new(py);

        // 基本资料
        let basic_dict = PyDict::new(py);
        for (k, v) in &parsed.basic_info {
            basic_dict.set_item(k, v)?;
        }
        dict.set_item("basic_info", basic_dict)?;

        // 发行上市信息
        let listing_dict = PyDict::new(py);
        for (k, v) in &parsed.listing_info {
            listing_dict.set_item(k, v)?;
        }
        dict.set_item("listing_info", listing_dict)?;

        // 章节内容
        let sections_dict = PyDict::new(py);
        for (k, v) in &parsed.sections {
            sections_dict.set_item(k, v)?;
        }
        dict.set_item("sections", sections_dict)?;

        Ok(dict.unbind())
    }

    /// 提取基本资料
    ///
    /// # 参数
    /// * `text` - F10 原始文本
    ///
    /// # 返回
    /// 基本资料字典
    #[staticmethod]
    fn extract_basic_info(py: Python<'_>, text: &str) -> PyResult<Py<PyDict>> {
        let info = extract_basic_info(text);

        let dict = PyDict::new(py);
        for (k, v) in &info {
            dict.set_item(k, v)?;
        }

        Ok(dict.unbind())
    }
}
