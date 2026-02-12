"""
配置管理模块
支持从 YAML 文件加载配置，并支持环境变量替换
"""
import os
import re
from pathlib import Path
from typing import Any, Dict, List
import yaml
from dotenv import load_dotenv


class Config:
    """配置管理类"""
    
    def __init__(self, config_path: str = None):
        """
        初始化配置
        
        Args:
            config_path: 配置文件路径，默认为项目根目录下的 config/config.yaml
        """
        # 加载 .env 文件
        load_dotenv()
        
        # 确定配置文件路径
        if config_path is None:
            project_root = Path(__file__).parent.parent
            config_path = project_root / "config" / "config.yaml"
        
        self.config_path = Path(config_path)
        self._config = self._load_config()
        
    def _load_config(self) -> Dict[str, Any]:
        """加载配置文件并替换环境变量"""
        if not self.config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {self.config_path}")
        
        with open(self.config_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 替换环境变量 ${VAR_NAME:default_value}
        content = self._substitute_env_vars(content)
        
        # 解析 YAML
        config = yaml.safe_load(content)
        
        return config
    
    def _substitute_env_vars(self, content: str) -> str:
        """
        替换配置内容中的环境变量
        
        支持格式：
        - ${VAR_NAME} - 必须存在的环境变量
        - ${VAR_NAME:default} - 带默认值的环境变量
        """
        def replacer(match):
            var_expr = match.group(1)
            
            # 解析变量名和默认值
            if ':' in var_expr:
                var_name, default = var_expr.split(':', 1)
            else:
                var_name = var_expr
                default = None
            
            # 获取环境变量值
            value = os.getenv(var_name.strip())
            
            if value is None:
                if default is not None:
                    # 处理 null 作为默认值的情况
                    if default.strip().lower() == 'null':
                        return ''
                    return default.strip()
                else:
                    raise ValueError(f"环境变量 {var_name} 未设置且没有默认值")
            
            return value
        
        # 替换 ${...} 格式的环境变量
        pattern = r'\$\{([^}]+)\}'
        return re.sub(pattern, replacer, content)
    
    @property
    def database(self) -> Dict[str, Any]:
        """数据库配置"""
        db_config = self._config.get('database', {})
        # 确保 port 是整数
        if 'port' in db_config:
            db_config['port'] = int(db_config['port'])
        return db_config
    
    @property
    def ingest(self) -> Dict[str, Any]:
        """采集配置"""
        return self._config.get('ingest', {})
    
    @property
    def logging(self) -> Dict[str, Any]:
        """日志配置"""
        return self._config.get('logging', {})
    
    @property
    def presets(self) -> List[Dict[str, Any]]:
        """Presets 配置列表"""
        return self._config.get('presets', [])
    
    @property
    def api_map_ids(self) -> Dict[str, Dict[int, str]]:
        """API Map IDs 配置"""
        return self._config.get('api_map_ids', {})
    
    def get_preset(self, presets_id: str) -> Dict[str, Any]:
        """
        根据 presets_id 获取配置
        
        Args:
            presets_id: preset ID
            
        Returns:
            preset 配置字典
            
        Raises:
            ValueError: 如果找不到指定的 preset
        """
        for preset in self.presets:
            if preset.get('presets_id') == presets_id:
                return preset
        
        raise ValueError(f"找不到 preset: {presets_id}")
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项"""
        return self._config.get(key, default)


# 全局配置实例（单例）
_config_instance: Config = None


def get_config(config_path: str = None) -> Config:
    """
    获取全局配置实例
    
    Args:
        config_path: 配置文件路径（仅首次调用时有效）
        
    Returns:
        Config 实例
    """
    global _config_instance
    
    if _config_instance is None:
        _config_instance = Config(config_path)
    
    return _config_instance


if __name__ == '__main__':
    # 测试代码
    config = get_config()
    print("数据库配置:", config.database)
    print("采集配置:", config.ingest)
    print("Presets 数量:", len(config.presets))
    for preset in config.presets:
        print(f"  - {preset['presets_id']}: {preset['area']}/{preset['environment']}")
