"""
数据库连接管理模块
支持单例连接和连接池
"""
import psycopg2
from psycopg2 import pool
from typing import Optional
from ..utils import logger


# 全局连接实例（单例模式）
_connection: Optional[psycopg2.extensions.connection] = None

# 全局连接池
_connection_pool: Optional[pool.SimpleConnectionPool] = None


def get_connection(db_config: dict, use_pool: bool = False) -> psycopg2.extensions.connection:
    """
    获取数据库连接（单例或从连接池获取）
    
    Args:
        db_config: 数据库配置字典，包含 host, port, database, user, password
        use_pool: 是否使用连接池
        
    Returns:
        数据库连接对象
    """
    if use_pool:
        return get_connection_from_pool(db_config)
    
    global _connection
    
    if _connection is None or _connection.closed:
        try:
            _connection = psycopg2.connect(**db_config)
            logger.info(f"数据库连接成功: {db_config['host']}:{db_config['port']}/{db_config['database']}")
        except Exception as e:
            logger.error(f"数据库连接失败: {e}")
            raise
    
    return _connection


def init_connection_pool(db_config: dict, min_conn: int = 1, max_conn: int = 20):
    """
    初始化数据库连接池
    
    Args:
        db_config: 数据库配置字典
        min_conn: 最小连接数
        max_conn: 最大连接数
    """
    global _connection_pool
    
    if _connection_pool is None:
        try:
            _connection_pool = pool.SimpleConnectionPool(
                minconn=min_conn,
                maxconn=max_conn,
                **db_config
            )
            logger.info(f"连接池初始化成功: min={min_conn}, max={max_conn}")
        except Exception as e:
            logger.error(f"连接池初始化失败: {e}")
            raise


def get_connection_from_pool(db_config: dict) -> psycopg2.extensions.connection:
    """
    从连接池获取连接
    
    Args:
        db_config: 数据库配置字典
        
    Returns:
        数据库连接对象
    """
    global _connection_pool
    
    if _connection_pool is None:
        # 自动初始化连接池
        init_connection_pool(db_config, min_conn=2, max_conn=20)
    
    try:
        conn = _connection_pool.getconn()
        return conn
    except Exception as e:
        logger.error(f"从连接池获取连接失败: {e}")
        raise


def return_connection_to_pool(conn: psycopg2.extensions.connection):
    """
    将连接归还到连接池
    
    Args:
        conn: 数据库连接对象
    """
    global _connection_pool
    
    if _connection_pool and conn:
        _connection_pool.putconn(conn)


def close_connection():
    """关闭数据库连接"""
    global _connection
    
    if _connection and not _connection.closed:
        _connection.close()
        logger.info("数据库连接已关闭")
        _connection = None


def close_connection_pool():
    """关闭连接池"""
    global _connection_pool
    
    if _connection_pool:
        _connection_pool.closeall()
        logger.info("连接池已关闭")
        _connection_pool = None
