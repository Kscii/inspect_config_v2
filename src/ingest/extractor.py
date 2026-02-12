"""
元数据提取模块
从 report JSON 中提取元数据
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
from ..utils import logger


class MetadataExtractor:
    """元数据提取器类"""
    
    @staticmethod
    def load_json(json_path: Path) -> Optional[Dict]:
        """
        加载 JSON 文件
        
        Args:
            json_path: JSON 文件路径
            
        Returns:
            解析后的 JSON 字典，或 None
        """
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data
        except Exception as e:
            logger.error(f"解析 JSON 失败 {json_path}: {e}")
            return None
    
    @staticmethod
    def extract_metadata(report_json: Dict) -> Dict:
        """
        从 report JSON 中提取元数据
        
        Args:
            report_json: 完整的 report JSON
            
        Returns:
            元数据字典，包含：
            - json_ver: JSON 版本
            - created_at: 报告创建时间
            - sn: 设备序列号
            - model: 设备型号
            - collected_at: 数据收集时间
        """
        metadata = {}
        
        # 提取顶层字段
        metadata['json_ver'] = report_json.get('reportVersion', 'unknown')
        metadata['created_at'] = report_json.get('createdAt')
        
        # 尝试从 metadata_raw 中提取设备信息
        try:
            for item in report_json.get('report', []):
                if item.get('ruleCode') == 'metadata_raw':
                    for metric in item.get('rawDataMetric', []):
                        if metric.get('name') == 'metadata.json':
                            raw_data = metric.get('rawData', {})
                            meta = raw_data.get('metadata', {})
                            
                            # 提取关键字段
                            equipment_info = meta.get('equipment_info', {})
                            metadata['sn'] = equipment_info.get('sn')
                            metadata['model'] = equipment_info.get('model')
                            metadata['collected_at'] = meta.get('collected_at')
                            
                            break
                if metadata.get('sn') and metadata.get('model'):
                    break
        except Exception as e:
            logger.warning(f"提取设备元数据失败: {e}")
        
        return metadata
    
    @staticmethod
    def parse_datetime(dt_str: Optional[str]) -> datetime:
        """
        解析时间字符串
        
        Args:
            dt_str: 时间字符串（ISO 格式）
            
        Returns:
            datetime 对象
        """
        if not dt_str:
            return datetime.now()
        
        try:
            # 处理 ISO 8601 格式（包含 Z）
            return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        except Exception:
            logger.warning(f"无法解析时间: {dt_str}，使用当前时间")
            return datetime.now()

