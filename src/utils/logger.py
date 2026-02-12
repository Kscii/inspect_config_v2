"""
日志工具模块
提供统一的日志配置和获取
"""
import logging
import sys
from pathlib import Path
from typing import Optional


def setup_logger(
    name: str = 'inspect_config',
    level: str = 'INFO',
    log_file: Optional[str] = None,
    console: bool = True
) -> logging.Logger:
    """
    设置日志记录器
    
    Args:
        name: 日志记录器名称
        level: 日志级别 (DEBUG, INFO, WARNING, ERROR)
        log_file: 日志文件路径，None 表示不写文件
        console: 是否输出到控制台
        
    Returns:
        配置好的 Logger 实例
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper()))
    
    # 避免重复添加 handler
    if logger.handlers:
        return logger
    
    # 日志格式
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 控制台输出
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    
    # 文件输出
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        file_handler = logging.FileHandler(log_path, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


def get_logger(name: str = 'inspect_config') -> logging.Logger:
    """
    获取日志记录器
    
    Args:
        name: 日志记录器名称
        
    Returns:
        Logger 实例
    """
    return logging.getLogger(name)


# 默认日志记录器
logger = setup_logger()


if __name__ == '__main__':
    # 测试代码
    test_logger = setup_logger('test', level='DEBUG', console=True)
    test_logger.debug('这是一条 DEBUG 消息')
    test_logger.info('这是一条 INFO 消息')
    test_logger.warning('这是一条 WARNING 消息')
    test_logger.error('这是一条 ERROR 消息')
