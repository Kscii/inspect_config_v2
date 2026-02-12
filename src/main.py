#!/usr/bin/env python3
"""
OBS 数据收集主程序
重构版本 v2.0
"""
import sys
from pathlib import Path
from typing import List, Optional

from .config import get_config
from .database import get_connection, close_connection, DatabaseRepository
from .ingest import DataProcessor, RangeImporter
from .ingest.field_extractor import FieldExtractor
from .utils import setup_logger, logger


def run_ingest(
    config,
    repository: DatabaseRepository
) -> None:
    """
    运行数据收集流程
    
    Args:
        config: 配置对象
        repository: 数据库仓库对象
    """
    # 获取所有 presets
    presets = config.presets
    
    if not presets:
        logger.error("没有要处理的 presets")
        return
    
    logger.info(f"将处理 {len(presets)} 个 presets")
    
    # 全局初始化：重置所有 episode 的 is_updated 标记
    logger.info("初始化 episode 更新标记...")
    repository.reset_all_episode_update_flags()
    
    # 创建数据处理器
    tmp_dir = Path(config.ingest.get('tmp_dir', '/tmp/obs_ingest'))
    processor = DataProcessor(config, repository, tmp_dir)
    
    # 处理每个 preset
    total_stats = {
        'listed': 0,
        'downloaded': 0,
        'inserted': 0,
        'errors': 0,
        'skipped': 0,
        'skipped_existing': 0,
        'skipped_parse_path': 0,
        'skipped_download': 0,
        'skipped_parse_json': 0,
        'skipped_missing_fields': 0,
        'skipped_duplicate': 0
    }
    
    for preset in presets:
        # 先同步 preset 到数据库
        processor.sync_preset_to_db(preset)
        
        # 处理数据收集
        stats = processor.process_preset(preset)
        
        # 累计统计
        for key in total_stats:
            total_stats[key] += stats.get(key, 0)
    
    # 输出总体统计
    logger.info(f"\n{'='*60}")
    logger.info("采集任务完成")
    logger.info(f"{'='*60}")
    logger.info(f"总计: 列出 {total_stats['listed']}, "
               f"下载 {total_stats['downloaded']}, "
               f"插入 {total_stats['inserted']}, "
               f"跳过 {total_stats['skipped']}, "
               f"错误 {total_stats['errors']}")
    
    # 输出详细的跳过分类统计
    if total_stats['skipped'] > 0:
        logger.info(f"跳过详情: "
                   f"已存在={total_stats['skipped_existing']}, "
                   f"路径解析失败={total_stats['skipped_parse_path']}, "
                   f"下载失败={total_stats['skipped_download']}, "
                   f"JSON解析失败={total_stats['skipped_parse_json']}, "
                   f"缺少字段={total_stats['skipped_missing_fields']}, "
                   f"重复={total_stats['skipped_duplicate']}")
    
    # 自动执行字段提取
    logger.info(f"\n{'='*60}")
    logger.info("开始自动提取字段...")
    logger.info(f"{'='*60}")
    run_field_extraction(config, repository)
    
    # 自动执行 range 导入
    logger.info(f"\n{'='*60}")
    logger.info("开始自动导入 Range 规则...")
    logger.info(f"{'='*60}")
    run_range_import(config, repository, tmp_dir)


def run_range_import(
    config,
    repository: DatabaseRepository,
    tmp_dir: Path
) -> None:
    """
    运行 range 导入流程
    
    Args:
        config: 配置对象
        repository: 数据库仓库对象
        tmp_dir: 临时目录
    """
    logger.info("开始 Range 导入流程...")
    
    try:
        # 创建 Range 导入器
        importer = RangeImporter(config, repository, tmp_dir)
        
        # 导入所有 range
        stats = importer.import_all_ranges()
        
        logger.info(f"\n{'='*60}")
        logger.info("Range 导入完成")
        logger.info(f"{'='*60}")
        logger.info(f"处理 Presets: {stats['total_presets']}")
        logger.info(f"处理 Models: {stats['total_models']}")
        logger.info(f"成功 Rules: {stats['success_rules']}")
        logger.info(f"失败 Rules: {stats['failed_rules']}")
        logger.info(f"插入 Field Ranges: {stats['total_field_ranges']}")
        
        if stats['errors']:
            logger.warning(f"遇到 {len(stats['errors'])} 个错误")
    
    except Exception as e:
        logger.error(f"Range 导入失败: {e}", exc_info=True)


def run_field_extraction(
    config,
    repository: DatabaseRepository
) -> None:
    """
    运行字段提取流程
    
    Args:
        config: 配置对象
        repository: 数据库仓库对象
    """
    logger.info("开始字段提取流程...")
    
    # 创建字段提取器
    extractor = FieldExtractor(config, repository)
    
    # 从数据库查询所有不同的 model
    cursor = repository.conn.cursor()
    try:
        cursor.execute("SELECT DISTINCT model FROM device ORDER BY model")
        models = [row[0] for row in cursor.fetchall()]
    finally:
        cursor.close()
    
    if not models:
        logger.error("没有找到任何 model")
        return
    
    logger.info(f"将处理 {len(models)} 个 model: {models}")
    
    # 处理每个 model
    for m in models:
        try:
            stats = extractor.extract_fields_for_model(m)
            logger.info(f"✓ 完成 model={m}: 字段={stats['fields_created']}, "
                       f"数值={stats['numeric_values']}, 文本={stats['text_values']}")
        except Exception as e:
            logger.error(f"✗ 提取失败 model={m}: {e}")


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
            name='inspect_config',
            level=log_level,
            log_file=log_file,
            console=console
        )
        
        logger.info("="*60)
        logger.info("OBS 数据收集系统 v2.0")
        logger.info("="*60)
        
        # 连接数据库
        db_config = config.database
        conn = get_connection(db_config)
        repository = DatabaseRepository(conn)
        
        # 执行数据收集流程
        run_ingest(config, repository)
        
        logger.info("程序执行完成")
        return 0
        
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
