use std::collections::VecDeque;
use std::sync::Mutex;

use crate::error::Result;
use crate::net::connection::TcpConnection;
use crate::protocol::constants::{CONNECT_TIMEOUT, DEFAULT_POOL_SIZE};
use crate::loge;

/// 连接池中的单个连接
struct PooledConnection {
    conn: TcpConnection,
    server: (String, u16),
}

/// 连接池配置
pub struct PoolConfig {
    pub max_size: usize,
    pub connect_timeout: f64,
    /// 握手回调: 新建连接后执行 (setup commands)
    pub handshake_fn: Option<Box<dyn Fn(&mut TcpConnection) -> Result<()> + Send + Sync>>,
}

impl PoolConfig {
    pub fn new() -> Self {
        Self {
            max_size: DEFAULT_POOL_SIZE,
            connect_timeout: CONNECT_TIMEOUT,
            handshake_fn: None,
        }
    }
}

impl Default for PoolConfig {
    fn default() -> Self {
        Self::new()
    }
}

/// 线程安全的连接池
///
/// 管理多个 TCP 连接，支持:
/// - 单服务器连接池 (多个连接到同一服务器)
/// - 多服务器连接池 (连接到不同服务器)
/// - 连接借出/归还
pub struct ConnectionPool {
    inner: Mutex<PoolInner>,
    config: PoolConfig,
}

struct PoolInner {
    idle: VecDeque<PooledConnection>,
    active: usize,
    total: usize,
}

impl ConnectionPool {
    /// 创建连接池 (单服务器)
    pub fn new_single(_server: (String, u16), config: PoolConfig) -> Self {
        Self {
            inner: Mutex::new(PoolInner {
                idle: VecDeque::new(),
                active: 0,
                total: 0,
            }),
            config,
        }
    }

    /// 将一个已握手的连接放入池中
    pub fn push(&self, conn: TcpConnection, server: (String, u16)) {
        let mut inner = self.inner.lock().unwrap();
        inner.total += 1;
        inner.idle.push_back(PooledConnection { conn, server });
    }

    /// 从池中借出一个连接
    ///
    /// 如果池中有空闲连接，返回一个；
    /// 如果未达上限，创建新连接；
    /// 如果已满，返回错误。
    pub fn borrow(&self, server: &(String, u16)) -> Result<PooledConnGuard<'_>> {
        let mut inner = self.inner.lock().unwrap();

        // 尝试从空闲队列获取
        if let Some(conn) = inner.idle.pop_front() {
            inner.active += 1;
            return Ok(PooledConnGuard {
                pool: self,
                conn: Some(conn),
            });
        }

        // 如果未达到上限，创建新连接
        if inner.total < self.config.max_size {
            let server_clone = server.clone();
            let has_handshake = self.config.handshake_fn.is_some();
            inner.total += 1;
            inner.active += 1;

            // 释放锁后再创建连接 (避免持锁做 I/O)
            drop(inner);

            let mut conn = match TcpConnection::connect(
                &server_clone.0,
                server_clone.1,
                self.config.connect_timeout,
            ) {
                Ok(c) => c,
                Err(e) => {
                    // 建连失败: 归还预留的计数, 否则幻影连接永久占用池
                    self.release_reservation();
                    return Err(e);
                }
            };

            // 执行握手 (如果有)
            if has_handshake {
                if let Some(ref handshake_fn) = self.config.handshake_fn {
                    if let Err(e) = handshake_fn(&mut conn) {
                        self.release_reservation();
                        return Err(e);
                    }
                }
            }

            return Ok(PooledConnGuard {
                pool: self,
                conn: Some(PooledConnection {
                    conn,
                    server: server_clone,
                }),
            });
        }

        loge!("pool", "exhausted (active={}, max={})", inner.active, self.config.max_size);
        Err(crate::error_codes::ErrorCode::POOL_EXHAUSTED.err(
            format!("active={}, max={}", inner.active, self.config.max_size)
        ))
    }

    /// 尝试借出连接 (非阻塞)
    pub fn try_borrow(&self, server: &(String, u16)) -> Result<Option<PooledConnGuard<'_>>> {
        let mut inner = self.inner.lock().unwrap();

        if let Some(conn) = inner.idle.pop_front() {
            inner.active += 1;
            return Ok(Some(PooledConnGuard {
                pool: self,
                conn: Some(conn),
            }));
        }

        if inner.total < self.config.max_size {
            let server_clone = server.clone();
            let has_handshake = self.config.handshake_fn.is_some();
            inner.total += 1;
            inner.active += 1;
            drop(inner);

            let mut conn = match TcpConnection::connect(
                &server_clone.0,
                server_clone.1,
                self.config.connect_timeout,
            ) {
                Ok(c) => c,
                Err(e) => {
                    self.release_reservation();
                    return Err(e);
                }
            };

            if has_handshake {
                if let Some(ref handshake_fn) = self.config.handshake_fn {
                    if let Err(e) = handshake_fn(&mut conn) {
                        self.release_reservation();
                        return Err(e);
                    }
                }
            }

            return Ok(Some(PooledConnGuard {
                pool: self,
                conn: Some(PooledConnection {
                    conn,
                    server: server_clone,
                }),
            }));
        }

        Ok(None)
    }

    /// 归还预留计数 (建连或握手失败时调用)
    ///
    /// `borrow`/`try_borrow` 在连接创建前预增 `total`/`active`,
    /// 失败路径必须归还, 否则幻影连接永久占用池直到 POOL_EXHAUSTED。
    fn release_reservation(&self) {
        let mut inner = self.inner.lock().unwrap();
        inner.total = inner.total.saturating_sub(1);
        inner.active = inner.active.saturating_sub(1);
    }

    /// 归还连接到池中
    fn return_connection(&self, pooled: PooledConnection) {
        let mut inner = self.inner.lock().unwrap();
        // saturating: close_all() 会将 active 清零, 此时仍借出中的连接
        // 归还时若做无符号减法将触发 usize 下溢 panic
        inner.active = inner.active.saturating_sub(1);

        if pooled.conn.is_open() && inner.idle.len() < self.config.max_size {
            inner.idle.push_back(pooled);
        } else {
            inner.total = inner.total.saturating_sub(1);
        }
    }

    /// 关闭所有连接
    pub fn close_all(&self) {
        let mut inner = self.inner.lock().unwrap();
        while let Some(mut conn) = inner.idle.pop_front() {
            conn.conn.close();
            inner.total -= 1;
        }
        inner.active = 0;
    }

    /// 获取池状态
    pub fn stats(&self) -> PoolStats {
        let inner = self.inner.lock().unwrap();
        PoolStats {
            idle: inner.idle.len(),
            active: inner.active,
            total: inner.total,
            max_size: self.config.max_size,
        }
    }
}

/// 连接池统计信息
#[derive(Debug, Clone)]
pub struct PoolStats {
    pub idle: usize,
    pub active: usize,
    pub total: usize,
    pub max_size: usize,
}

/// 借出的连接守卫 (自动归还)
pub struct PooledConnGuard<'a> {
    pool: &'a ConnectionPool,
    conn: Option<PooledConnection>,
}

impl<'a> PooledConnGuard<'a> {
    /// 获取连接引用
    pub fn conn(&mut self) -> &mut TcpConnection {
        &mut self.conn.as_mut().unwrap().conn
    }

    /// 获取服务器信息
    pub fn server(&self) -> &(String, u16) {
        &self.conn.as_ref().unwrap().server
    }
}

impl<'a> Drop for PooledConnGuard<'a> {
    fn drop(&mut self) {
        if let Some(conn) = self.conn.take() {
            self.pool.return_connection(conn);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::TdxError;

    #[test]
    fn test_pool_stats_initial() {
        let config = PoolConfig::new();
        let pool = ConnectionPool::new_single(("127.0.0.1".to_string(), 7709), config);
        let stats = pool.stats();
        assert_eq!(stats.idle, 0);
        assert_eq!(stats.active, 0);
        assert_eq!(stats.total, 0);
        assert_eq!(stats.max_size, DEFAULT_POOL_SIZE);
    }

    #[test]
    fn test_pool_config_default() {
        let config = PoolConfig::default();
        assert_eq!(config.max_size, DEFAULT_POOL_SIZE);
        assert_eq!(config.connect_timeout, CONNECT_TIMEOUT);
    }

    #[test]
    fn test_pool_borrow_failure_no_server() {
        let mut config = PoolConfig::new();
        config.max_size = 2;
        config.connect_timeout = 0.1;
        let pool = ConnectionPool::new_single(("127.0.0.1".to_string(), 1), config);
        let server = ("127.0.0.1".to_string(), 1);
        let result = pool.borrow(&server);
        assert!(result.is_err());
    }

    #[test]
    fn test_pool_close_all() {
        let config = PoolConfig::new();
        let pool = ConnectionPool::new_single(("127.0.0.1".to_string(), 7709), config);
        pool.close_all();
        let stats = pool.stats();
        assert_eq!(stats.total, 0);
    }

    #[test]
    fn test_pool_borrow_failure_rolls_back_counters() {
        use crate::error_codes::ErrorCode;

        let mut config = PoolConfig::new();
        config.max_size = 2;
        config.connect_timeout = 0.2;
        let pool = ConnectionPool::new_single(("127.0.0.1".to_string(), 1), config);
        let server = ("127.0.0.1".to_string(), 1);

        // 连续失败远超 max_size 次: 若计数泄漏, 第 3 次起将永远 POOL_EXHAUSTED (池报废)
        for i in 0..6 {
            let err = match pool.borrow(&server) {
                Err(e) => e,
                Ok(_) => panic!("第 {} 次不应成功", i + 1),
            };
            assert_ne!(
                err.error_code(),
                Some(ErrorCode::POOL_EXHAUSTED),
                "第 {} 次失败后池被幻影连接占满",
                i + 1
            );
            let stats = pool.stats();
            assert_eq!(stats.total, 0, "total 计数泄漏 (第 {} 次)", i + 1);
            assert_eq!(stats.active, 0, "active 计数泄漏 (第 {} 次)", i + 1);
        }
    }

    #[test]
    fn test_pool_handshake_failure_rolls_back_counters() {
        use std::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let server = (addr.ip().to_string(), addr.port());

        let mut config = PoolConfig::new();
        config.max_size = 2;
        config.connect_timeout = 2.0;
        config.handshake_fn = Some(Box::new(|_conn| Err(TdxError::SetupFailed("test".into()))));
        let pool = ConnectionPool::new_single(server.clone(), config);

        // connect 成功但 handshake 失败: 预留的计数必须回滚
        for i in 0..4 {
            let err = match pool.borrow(&server) {
                Err(e) => e,
                Ok(_) => panic!("第 {} 次不应成功", i + 1),
            };
            assert!(matches!(err, TdxError::SetupFailed(_)), "第 {} 次", i + 1);
            let stats = pool.stats();
            assert_eq!(stats.total, 0, "total 计数泄漏 (第 {} 次)", i + 1);
            assert_eq!(stats.active, 0, "active 计数泄漏 (第 {} 次)", i + 1);
        }
    }

    #[test]
    fn test_return_after_close_all_no_underflow() {
        use std::net::TcpListener;

        let config = PoolConfig::new();
        let pool = ConnectionPool::new_single(("127.0.0.1".to_string(), 7709), config);

        // 本机起 listener 使 connect 成功, 构造 "借出中" 状态
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let conn =
            TcpConnection::connect(&addr.ip().to_string(), addr.port(), 2.0).unwrap();
        pool.push(conn, ("127.0.0.1".to_string(), 7709));

        let guard = pool
            .borrow(&("127.0.0.1".to_string(), 7709))
            .expect("借出应成功");
        pool.close_all();
        drop(guard); // 修复前: active 0-1 → usize 下溢 panic

        let stats = pool.stats();
        assert_eq!(stats.active, 0);
    }
}
