/// F10 公司资料客户端

use crate::error::Result;
use crate::net::client::TdxHqClient;
use crate::net::utils::{auto_market, encode_gbk, encode_gbk_padded};
use crate::protocol::constants::{MARKET_SH, MARKET_SZ};
use crate::loge;
use super::constants::*;
use super::parser::*;
use super::types::*;

/// F10 公司资料客户端
///
/// 提供获取通达信 F10 公司基本面资料数据的能力。
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
pub struct ProfileClient<'a> {
    client: &'a mut TdxHqClient,
}

impl<'a> ProfileClient<'a> {
    /// 创建新的 ProfileClient
    ///
    /// # 参数
    /// * `client` - TdxHqClient 实例的可变引用
    pub fn new(client: &'a mut TdxHqClient) -> Self {
        Self { client }
    }

    /// 获取 F10 分类列表
    ///
    /// # 参数
    /// * `market` - 市场代码 (0=SZ, 1=SH)
    /// * `code` - 股票代码
    ///
    /// # 返回
    /// 分类列表，包含每个分类的名称、文件名、起始位置和长度
    ///
    /// # 示例
    ///
    /// ```ignore
    /// let categories = profile.get_category(1, "600519")?;
    /// for cat in &categories {
    ///     println!("{}", cat.name);
    /// }
    /// ```
    pub fn get_category(&mut self, market: u8, code: &str) -> Result<Vec<F10Category>> {
        // 验证市场代码
        if market != MARKET_SZ && market != MARKET_SH {
            return Err(crate::error::TdxError::InvalidData(format!(
                "无效的市场代码: {} (仅支持 0=SZ, 1=SH)",
                market
            )));
        }

        // 验证股票代码
        if code.len() != 6 {
            return Err(crate::error::TdxError::InvalidData(format!(
                "无效的股票代码: {} (必须为 6 位数字)",
                code
            )));
        }

        // 构建请求包
        let mut pkg = Vec::with_capacity(24);
        pkg.extend_from_slice(&CATEGORY_REQUEST_HEADER);
        pkg.extend_from_slice(&(market as u16).to_le_bytes());

        // 股票代码 (GBK, 6 字节)
        let code_gbk = encode_gbk(code)?;
        pkg.extend_from_slice(&code_gbk);

        // 保留字段 (u32, 0)
        pkg.extend_from_slice(&0u32.to_le_bytes());

        // 发送请求并接收响应
        let response = self.send_and_recv(&pkg)?;

        // 解析响应
        parse_company_info_category(&response)
    }

    /// 获取 F10 分类列表 (自动识别市场)
    ///
    /// # 参数
    /// * `code` - 股票代码
    ///
    /// # 返回
    /// 分类列表
    pub fn get_category_auto(&mut self, code: &str) -> Result<Vec<F10Category>> {
        let market = auto_market(code)?;
        self.get_category(market, code)
    }

    /// 获取 F10 内容
    ///
    /// # 参数
    /// * `market` - 市场代码
    /// * `code` - 股票代码
    /// * `category` - 分类信息 (从 get_category 获取)
    ///
    /// # 返回
    /// 文本内容
    pub fn get_content(
        &mut self,
        market: u8,
        code: &str,
        category: &F10Category,
    ) -> Result<F10Content> {
        // 验证市场代码
        if market != MARKET_SZ && market != MARKET_SH {
            return Err(crate::error::TdxError::InvalidData(format!(
                "无效的市场代码: {} (仅支持 0=SZ, 1=SH)",
                market
            )));
        }

        // 验证股票代码
        if code.len() != 6 {
            return Err(crate::error::TdxError::InvalidData(format!(
                "无效的股票代码: {} (必须为 6 位数字)",
                code
            )));
        }

        // 构建请求包
        let mut pkg = Vec::with_capacity(104);
        pkg.extend_from_slice(&CONTENT_REQUEST_HEADER);
        pkg.extend_from_slice(&(market as u16).to_le_bytes());

        // 股票代码 (GBK, 6 字节)
        let code_gbk = encode_gbk(code)?;
        pkg.extend_from_slice(&code_gbk);

        // 保留字段 (u16, 0)
        pkg.extend_from_slice(&0u16.to_le_bytes());

        // 文件名 (GBK, 80 字节, null 填充) — 优先使用原始字节避免编码损耗
        let filename_gbk = if category.filename_raw.is_empty() {
            encode_gbk_padded(&category.filename, CATEGORY_FILENAME_SIZE)?
        } else {
            let mut raw = category.filename_raw.clone();
            if raw.len() < CATEGORY_FILENAME_SIZE {
                raw.resize(CATEGORY_FILENAME_SIZE, 0);
            } else if raw.len() > CATEGORY_FILENAME_SIZE {
                raw.truncate(CATEGORY_FILENAME_SIZE);
            }
            raw
        };
        pkg.extend_from_slice(&filename_gbk);

        // 起始位置 (u32)
        pkg.extend_from_slice(&category.start.to_le_bytes());

        // 数据长度 (u32)
        pkg.extend_from_slice(&category.length.to_le_bytes());

        // 保留字段 (u32, 0)
        pkg.extend_from_slice(&0u32.to_le_bytes());

        // 发送请求并接收响应
        let response = self.send_and_recv(&pkg)?;

        // 解析响应
        let content = parse_company_info_content(&response)?;

        Ok(F10Content::new(
            category.name.clone(),
            content,
        ))
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
    pub fn get_content_by_name(
        &mut self,
        market: u8,
        code: &str,
        name: &str,
    ) -> Result<F10Content> {
        // 先获取分类列表
        let categories = self.get_category(market, code)?;

        // 查找指定分类
        let category = categories
            .iter()
            .find(|c| c.name == name)
            .ok_or_else(|| {
                crate::error::TdxError::InvalidData(format!(
                    "未找到分类 '{}'，可用分类: {}",
                    name,
                    categories
                        .iter()
                        .map(|c| c.name.as_str())
                        .collect::<Vec<_>>()
                        .join(", ")
                ))
            })?;

        self.get_content(market, code, category)
    }

    /// 获取所有分类的内容
    ///
    /// # 参数
    /// * `market` - 市场代码
    /// * `code` - 股票代码
    ///
    /// # 返回
    /// 所有分类的内容列表
    pub fn get_all_contents(
        &mut self,
        market: u8,
        code: &str,
    ) -> Result<Vec<F10Content>> {
        let categories = self.get_category(market, code)?;
        let mut contents = Vec::with_capacity(categories.len());

        for category in &categories {
            match self.get_content(market, code, category) {
                Ok(content) => contents.push(content),
                Err(e) => {
                    loge!("profile", "获取分类 '{}' 失败: {}", category.name, e);
                }
            }
        }

        Ok(contents)
    }

    /// 获取所有分类的内容 (返回 F10Data)
    ///
    /// # 参数
    /// * `market` - 市场代码
    /// * `code` - 股票代码
    ///
    /// # 返回
    /// F10Data 包含所有分类的内容
    pub fn get_all_data(
        &mut self,
        market: u8,
        code: &str,
    ) -> Result<F10Data> {
        let contents = self.get_all_contents(market, code)?;
        let mut data = F10Data::new(code.to_string(), market);
        for content in contents {
            data.add_content(content);
        }
        Ok(data)
    }

    /// 发送请求并接收响应 (内部方法)
    fn send_and_recv(&mut self, pkg: &[u8]) -> Result<Vec<u8>> {
        self.client.send_raw_and_recv(pkg)
    }
}
