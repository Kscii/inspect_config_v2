"""
字段提取器模块
职责：
1) 扫描 json_report 表中的数据
2) 生成 field 定义并批量插入
3) 提取字段值并批量插入 field_value_num/text 表
"""
import json
import time
from pathlib import Path
from typing import Dict, List, Set, Tuple
from ..utils import logger
from ..config import Config
from ..database import DatabaseRepository
from .field_parser import (
    generate_selectors_from_json,
    extract_value_by_selector,
    extract_rule_code,
    extract_field_name,
    extract_field_type,
    determine_data_type,
    value_to_string,
    apply_filters
)


class FieldExtractor:
    """字段提取器类"""
    
    def __init__(self, config: Config, repository: DatabaseRepository):
        """
        初始化字段提取器
        
        Args:
            config: 配置对象
            repository: 数据库仓库对象
        """
        self.config = config
        self.repository = repository
        self.field_batch_size = config.ingest.get('field_batch_size', 1000)
    
    def extract_fields_for_model(self, model: str) -> Dict[str, int]:
        """
        为指定 model 提取字段
        
        Args:
            model: 机器人型号
            
        Returns:
            统计信息字典
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"开始字段提取: model={model}")
        logger.info(f"{'='*60}")
        
        start_time_total = time.time()
        
        stats = {
            'episodes_scanned': 0,
            'fields_created': 0,
            'numeric_values': 0,
            'text_values': 0,
            'errors': 0
        }
        
        try:
            # 清理被更新 episode 的旧字段值（不删除 field 定义，可复用）
            logger.info(f"[字段提取] 阶段0: 清理被更新 episode 的旧字段值...")
            start_time = time.time()
            
            cleanup_stats = self.repository.cleanup_updated_episode_field_values(model)
            
            elapsed = time.time() - start_time
            logger.info(
                f"⏱️  数据清理完成: "
                f"删除 {cleanup_stats['field_value_num_deleted']} 条数值, "
                f"{cleanup_stats['field_value_text_deleted']} 条文本, "
                f"耗时 {elapsed:.2f}秒"
            )
            
            # 第一遍扫描：收集所有唯一字段
            logger.info(f"[字段提取] 阶段1: 收集字段定义...")
            start_time = time.time()
            
            filter_config = self.config.ingest.get('field_filter', {})
            if filter_config.get('enable_filter', False):
                logger.info(f"[字段提取] 过滤配置已启用 (model={model})")
            
            field_definitions = self._collect_field_definitions(model)
            stats['fields_created'] = len(field_definitions)
            
            elapsed = time.time() - start_time
            logger.info(f"⏱️  字段收集完成: {len(field_definitions)} 个唯一字段，耗时 {elapsed:.2f}秒")
            
            if not field_definitions:
                logger.warning(f"[字段提取] model={model} 未找到任何字段定义")
                return stats
            
            # 批量插入字段定义到 field 表
            logger.info(f"[字段提取] 批量插入字段定义到数据库...")
            start_time = time.time()
            
            field_id_map = self.repository.batch_insert_fields(field_definitions)
            
            elapsed = time.time() - start_time
            logger.info(f"⏱️  字段定义插入完成: 耗时 {elapsed:.2f}秒")
            
            # 第二遍扫描：提取所有字段值
            logger.info(f"[字段提取] 阶段2: 提取字段值...")
            start_time = time.time()
            
            value_stats = self._extract_field_values(model, field_id_map)
            stats.update(value_stats)
            
            elapsed = time.time() - start_time
            logger.info(f"⏱️  字段值提取完成: 耗时 {elapsed:.2f}秒")
            
            # 总结
            total_elapsed = time.time() - start_time_total
            logger.info(f"\n完成字段提取 {model}:")
            logger.info(f"  - 扫描 episode: {stats['episodes_scanned']}")
            logger.info(f"  - 创建字段: {stats['fields_created']}")
            logger.info(f"  - 数值型字段值: {stats['numeric_values']}")
            logger.info(f"  - 文本型字段值: {stats['text_values']}")
            logger.info(f"  - 错误数: {stats['errors']}")
            logger.info(f"⏱️  总耗时: {total_elapsed:.2f}秒 ({total_elapsed/60:.1f}分钟)")
            
            return stats
            
        except Exception as e:
            logger.error(f"字段提取失败 model={model}: {e}")
            raise
    
    def _collect_field_definitions(self, model: str) -> List[Dict]:
        """
        第一遍扫描：收集所有唯一字段定义
        优化：每个 task 只扫描一个被更新的 episode（大幅减少扫描量）
        
        Args:
            model: 机器人型号
            
        Returns:
            字段定义列表
        """
        # 从数据库获取每个 task 的一个被更新的 episode（使用 DISTINCT ON）
        cursor = self.repository.conn.cursor()
        try:
            sql = """
                SELECT DISTINCT ON (e.task_id) e.episode_id, jr.json_report, e.task_id
                FROM episode e
                JOIN device d ON e.device_id = d.device_id
                JOIN json_report jr ON e.episode_id = jr.episode_id
                WHERE d.model = %s AND e.is_updated = true
                ORDER BY e.task_id, e.episode_id
            """
            
            cursor.execute(sql, (model,))
            
            # 收集所有唯一字段
            field_map: Dict[str, Dict] = {}  # field_key -> field_info
            sample_values: Dict[str, any] = {}  # field_key -> sample_value（用于判断类型）
            
            row_count = 0
            for row in cursor.fetchall():
                row_count += 1
                episode_id = row[0]
                json_str = row[1]
                
                try:
                    # 解析 JSON
                    if isinstance(json_str, str):
                        json_data = json.loads(json_str)
                    else:
                        json_data = json_str
                    
                    # 获取过滤配置
                    filter_config = self.config.ingest.get('field_filter', {})
                    preferred_filter_keys = filter_config.get('preferred_filter_keys', None)
                    
                    # 生成 selectors（传入优先 key 列表）
                    raw_selectors = generate_selectors_from_json(json_data, preferred_filter_keys)
                    
                    # 应用过滤规则
                    selectors = apply_filters(raw_selectors, model, filter_config)
                    
                    # 调试日志（只在第一行输出）
                    if row_count == 1:
                        if filter_config.get('enable_filter', False):
                            logger.debug(f"[字段过滤] 原始字段: {len(raw_selectors)}, 过滤后: {len(selectors)}")
                        if preferred_filter_keys:
                            logger.info(f"[字段提取] 使用优先 filter keys: {preferred_filter_keys}")
                    
                    # 处理每个 selector
                    for selector in selectors:
                        if selector not in field_map:
                            # 提取字段元数据
                            rule_code = extract_rule_code(selector)
                            field_name = extract_field_name(selector)
                            field_type = extract_field_type(selector)
                            
                            # 提取一个样本值用于判断类型
                            value = extract_value_by_selector(json_data, selector)
                            sample_values[selector] = value
                            
                            # 判断数据类型
                            data_type = determine_data_type(value, rule_code)
                            
                            field_map[selector] = {
                                'field_key': selector,
                                'field_name': field_name,
                                'rule_code': rule_code,
                                'field_type': field_type,
                                'data_type': data_type,
                                'model': model,
                                'is_selected': True
                            }
                    
                    if row_count % 100 == 0:
                        logger.info(f"  已扫描 {row_count} 个 task，发现 {len(field_map)} 个唯一字段")
                        
                except Exception as e:
                    logger.error(f"处理 task 失败 task_id={row[2] if len(row) > 2 else 'unknown'} episode_id={episode_id}: {e}")
                    continue
            
            logger.info(f"  扫描完成：共 {row_count} 个 task（每个 task 1个样本 episode），发现 {len(field_map)} 个唯一字段")
            
            return list(field_map.values())
            
        finally:
            cursor.close()
    
    def _extract_field_values(
        self,
        model: str,
        field_id_map: Dict[str, int]
    ) -> Dict[str, int]:
        """
        第二遍扫描：提取所有字段值
        
        Args:
            model: 机器人型号
            field_id_map: field_key -> field_id 的映射
            
        Returns:
            统计信息字典
        """
        stats = {
            'episodes_scanned': 0,
            'numeric_values': 0,
            'text_values': 0,
            'errors': 0
        }
        
        # 从数据库获取 episode_id 和 json_report（只查询被更新的）
        cursor = self.repository.conn.cursor()
        try:
            sql = """
                SELECT e.episode_id, jr.json_report, jr.json_report_ver
                FROM episode e
                JOIN device d ON e.device_id = d.device_id
                JOIN json_report jr ON e.episode_id = jr.episode_id
                WHERE d.model = %s AND e.is_updated = true
                ORDER BY e.episode_id
            """
            
            cursor.execute(sql, (model,))
            
            # 批量缓冲区
            numeric_buffer = []
            text_buffer = []
            
            for row in cursor.fetchall():
                episode_id = row[0]
                json_str = row[1]
                
                try:
                    # 解析 JSON
                    if isinstance(json_str, str):
                        json_data = json.loads(json_str)
                    else:
                        json_data = json_str
                    
                    # 提取所有字段值
                    for field_key, field_id in field_id_map.items():
                        value = extract_value_by_selector(json_data, field_key)
                        
                        if value is None:
                            continue
                        
                        # 根据字段定义的类型判断插入哪个表
                        # 重新判断类型（因为可能有不同的值）
                        rule_code = extract_rule_code(field_key)
                        data_type = determine_data_type(value, rule_code)
                        
                        value_str = value_to_string(value)
                        
                        if data_type == "numeric":
                            try:
                                # 尝试转换为数字
                                if isinstance(value, (int, float)) and not isinstance(value, bool):
                                    numeric_value = value
                                elif isinstance(value, str):
                                    numeric_value = float(value) if '.' in value or 'e' in value.lower() else int(value)
                                else:
                                    continue
                                
                                numeric_buffer.append({
                                    'episode_id': episode_id,
                                    'field_id': field_id,
                                    'value': numeric_value
                                })
                            except (ValueError, TypeError):
                                # 转换失败，作为文本存储
                                text_buffer.append({
                                    'episode_id': episode_id,
                                    'field_id': field_id,
                                    'value': value_str
                                })
                        else:
                            text_buffer.append({
                                'episode_id': episode_id,
                                'field_id': field_id,
                                'value': value_str
                            })
                    
                    stats['episodes_scanned'] += 1
                    
                    # 达到批量大小时提交
                    if len(numeric_buffer) >= self.field_batch_size:
                        count = self.repository.batch_insert_field_values_num(numeric_buffer)
                        stats['numeric_values'] += count
                        numeric_buffer.clear()
                    
                    if len(text_buffer) >= self.field_batch_size:
                        count = self.repository.batch_insert_field_values_text(text_buffer)
                        stats['text_values'] += count
                        text_buffer.clear()
                    
                    if stats['episodes_scanned'] % 1000 == 0:
                        logger.info(f"  已处理 {stats['episodes_scanned']} 个 episode")
                        
                except Exception as e:
                    logger.error(f"提取字段值失败 {episode_id}: {e}")
                    stats['errors'] += 1
                    continue
            
            # 提交剩余数据
            if numeric_buffer:
                count = self.repository.batch_insert_field_values_num(numeric_buffer)
                stats['numeric_values'] += count
            
            if text_buffer:
                count = self.repository.batch_insert_field_values_text(text_buffer)
                stats['text_values'] += count
            
            return stats
            
        finally:
            cursor.close()
