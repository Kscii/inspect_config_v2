"""
Range CSV 解析器模块
用于解析 OBS 上的 range CSV 文件（格式：field,min,max,pass_count,fail_count,pass_rate）
"""
import csv
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from ..utils import logger


class RangeParser:
    """Range CSV 文件解析器"""
    
    @staticmethod
    def parse_range_path(obs_key: str) -> Optional[Dict[str, str]]:
        """
        解析 range 文件路径，提取元数据
        
        路径格式: range/{model}/{base/full}/{time}/{time}_range_{base/full}.csv
        或 obs://bucket/range/{model}/{base/full}/{time}/{time}_range_{base/full}.csv
        时间格式: 20260209_131649
        
        Args:
            obs_key: OBS 文件路径（可能包含 obs:// 前缀）
            
        Returns:
            包含 model, rule_type, time_str, datetime 的字典，失败返回 None
        """
        try:
            # 去掉 obs:// 前缀和 bucket 名称
            path_str = obs_key
            if path_str.startswith('obs://'):
                # obs://bucket/range/... -> range/...
                path_str = '/'.join(path_str.split('/')[3:])
            
            parts = Path(path_str).parts
            
            # 验证路径结构
            if len(parts) < 5 or parts[0] != 'range':
                return None
            
            model = parts[1]
            rule_type = parts[2]  # base 或 full
            time_str = parts[3]
            filename = parts[4]
            
            # 验证 rule_type
            if rule_type not in ('base', 'full'):
                logger.warning(f"无效的 rule_type: {rule_type}, 路径: {obs_key}")
                return None
            
            # 验证文件名格式: {time}_range_{base/full}.csv
            expected_filename = f"{time_str}_range_{rule_type}.csv"
            if filename != expected_filename:
                logger.warning(f"文件名格式不匹配: 期望 {expected_filename}, 实际 {filename}")
                return None
            
            # 解析时间字符串 (20260209_131649)
            try:
                dt = datetime.strptime(time_str, "%Y%m%d_%H%M%S")
            except ValueError:
                logger.warning(f"无效的时间格式: {time_str}, 路径: {obs_key}")
                return None
            
            return {
                'model': model,
                'rule_type': rule_type,
                'time_str': time_str,
                'datetime': dt,
                'obs_key': obs_key
            }
            
        except Exception as e:
            logger.error(f"解析 range 路径失败 {obs_key}: {e}")
            return None
    
    @staticmethod
    def parse_range_csv(csv_path: Path) -> List[Dict[str, Optional[float]]]:
        """
        解析 range CSV 文件
        
        CSV 格式: field,min,max,pass_count,fail_count,pass_rate
        只提取 field, min, max 三列
        
        Args:
            csv_path: CSV 文件路径
            
        Returns:
            字段范围列表，每个元素包含 field, min, max
        """
        ranges = []
        
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                
                # 验证必需的列
                if not all(col in reader.fieldnames for col in ['field', 'min', 'max']):
                    logger.error(f"CSV 缺少必需的列: {csv_path}")
                    return []
                
                for row_num, row in enumerate(reader, start=2):  # 从2开始（第1行是表头）
                    try:
                        field = row.get('field', '').strip()
                        if not field:
                            logger.warning(f"{csv_path}:{row_num} - field 为空，跳过")
                            continue
                        
                        # 解析 min 和 max（可能为空）
                        min_val = RangeParser._parse_number(row.get('min', ''))
                        max_val = RangeParser._parse_number(row.get('max', ''))
                        
                        ranges.append({
                            'field': field,
                            'min': min_val,
                            'max': max_val
                        })
                        
                    except Exception as e:
                        logger.warning(f"{csv_path}:{row_num} - 解析行失败: {e}")
                        continue
            
            logger.info(f"成功解析 {len(ranges)} 条字段范围: {csv_path}")
            return ranges
            
        except Exception as e:
            logger.error(f"读取 CSV 文件失败 {csv_path}: {e}")
            return []
    
    @staticmethod
    def _parse_number(value: str) -> Optional[float]:
        """
        解析数字字符串
        
        Args:
            value: 数字字符串
            
        Returns:
            浮点数或 None（如果为空或无效）
        """
        if not value or not value.strip():
            return None
        
        try:
            return float(value.strip())
        except ValueError:
            logger.warning(f"无法解析为数字: '{value}'")
            return None
    
    @staticmethod
    def get_latest_range_file(file_list: List[str], model: str, rule_type: str) -> Optional[Tuple[str, datetime]]:
        """
        从文件列表中获取指定 model 和 rule_type 的最新 range 文件
        
        Args:
            file_list: OBS 文件路径列表
            model: 机器人型号
            rule_type: 规则类型 (base/full)
            
        Returns:
            (最新文件路径, 时间) 或 None
        """
        latest_file = None
        latest_time = None
        
        for obs_key in file_list:
            metadata = RangeParser.parse_range_path(obs_key)
            
            if metadata and metadata['model'] == model and metadata['rule_type'] == rule_type:
                file_time = metadata['datetime']
                
                if latest_time is None or file_time > latest_time:
                    latest_time = file_time
                    latest_file = obs_key
        
        if latest_file:
            logger.info(f"找到最新 range 文件: {latest_file} ({latest_time})")
            return latest_file, latest_time
        
        return None
