"""
OBS 路径解析模块
解析 OBS 路径，提取 task_id 和 episode_id
"""
import re
from typing import Optional, Dict
from ..utils import logger


class PathParser:
    """路径解析器类"""
    
    # 带连字符的32位hex格式
    PATTERN_DASHED = r'obs://[^/]+/.*?/collect/([0-9a-f]{32})/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/\2_collect\.json'
    
    # s1 开头的32位 hex 格式
    PATTERN_S1 = r'obs://[^/]+/.*?/collect/([0-9a-f]{32})/(s1[0-9a-f]{30})/\2_collect\.json'
    
    # 通用模式
    PATTERN_GENERIC = r'obs://[^/]+/.*?/collect/([0-9a-f]{32})/([^/]+)/\2_collect\.json'
    
    @classmethod
    def parse(cls, obs_path: str) -> Optional[Dict[str, str]]:
        """
        解析 OBS 路径，提取 task_id 和 episode_id
        
        路径格式：obs://bucket/data-collector-svc/collect/{task_id}/{episode_id}/{episode_id}_collect.json
        
        支持的 episode_id 格式：
        1. 带连字符的32位hex: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
        2. 不带连字符的32位hex: s1xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
        3. 其他格式的通用匹配
        
        Args:
            obs_path: OBS 完整路径
            
        Returns:
            包含 task_id 和 episode_id 的字典，或 None
        """
        # 尝试匹配带连字符的格式
        match = re.search(cls.PATTERN_DASHED, obs_path, re.IGNORECASE)
        if match:
            return {
                'task_id': match.group(1),
                'episode_id': match.group(2)
            }
        
        # 尝试匹配 s1 开头的格式
        match = re.search(cls.PATTERN_S1, obs_path, re.IGNORECASE)
        if match:
            return {
                'task_id': match.group(1),
                'episode_id': match.group(2)
            }
        
        # 尝试通用模式
        match = re.search(cls.PATTERN_GENERIC, obs_path, re.IGNORECASE)
        if match:
            return {
                'task_id': match.group(1),
                'episode_id': match.group(2)
            }
        
        logger.warning(f"无法解析路径: {obs_path}")
        return None

