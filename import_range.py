#!/usr/bin/env python3
"""
Range 导入独立工具
单独运行 Range 规则导入，不执行数据收集
"""
import sys
from pathlib import Path

from src.config import get_config
from src.database import get_connection, close_connection, DatabaseRepository
from src.ingest import RangeImporter
from src.utils import setup_logger, logger


def main():
    """主函数"""
    try:
        # 加载配置
        config = get_config()
        
        # 设置日志
        log_config = config.logging
        log_level = log_config.get('level', 'INFO')
        log_file = log_config.get('file') if log_config.get('file') else None
        console = log_config.get('console', True)
        
        setup_logger(
            name='range_import',
            level=log_level,
            log_file=log_file,
            console=console
        )
        
        logger.info("="*80)
        logger.info("Range 规则导入工具")
        logger.info("="*80)
        
        # 连接数据库
        db_config = config.database
        conn = get_connection(db_config)
        repository = DatabaseRepository(conn)
        
        # 创建临时目录
        tmp_dir = Path(config.ingest.get('tmp_dir', '/tmp/obs_ingest'))
        tmp_dir.mkdir(parents=True, exist_ok=True)
        
        # 创建 Range 导入器
        importer = RangeImporter(config, repository, tmp_dir)
        
        # 导入所有 range
        stats = importer.import_all_ranges()
        
        # 输出结果
        logger.info(f"\n{'='*80}")
        logger.info("导入完成")
        logger.info(f"{'='*80}")
        logger.info(f"处理 Presets: {stats['total_presets']}")
        logger.info(f"处理 Models: {stats['total_models']}")
        logger.info(f"处理 Rules: {stats['total_rules']}")
        logger.info(f"  - 成功: {stats['success_rules']}")
        logger.info(f"  - 失败: {stats['failed_rules']}")
        logger.info(f"插入 Field Ranges: {stats['total_field_ranges']}")
        
        if stats['errors']:
            logger.warning(f"\n遇到 {len(stats['errors'])} 个错误:")
            for i, error in enumerate(stats['errors'][:20], 1):  # 最多显示20个
                logger.warning(f"  {i}. {error}")
            if len(stats['errors']) > 20:
                logger.warning(f"  ... 还有 {len(stats['errors']) - 20} 个错误未显示")
        
        logger.info("="*80)
        
        return 0 if stats['failed_rules'] == 0 else 1
        
    except KeyboardInterrupt:
        logger.warning("\n程序被用户中断")
        return 130
    except Exception as e:
        logger.error(f"程序执行失败: {e}", exc_info=True)
        return 1
    finally:
        # 关闭数据库连接
        close_connection()


if __name__ == '__main__':
    sys.exit(main())
