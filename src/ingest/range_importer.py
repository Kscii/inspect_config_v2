"""
Range 导入器模块
从 OBS 读取 range CSV 文件并导入到数据库
"""
import time
from pathlib import Path
from typing import Dict, List, Tuple
from collections import defaultdict
from ..utils import logger
from ..config import Config
from ..database import DatabaseRepository
from ..obs import ObsClient
from .range_parser import RangeParser


class RangeImporter:
    """Range 导入器类"""
    
    def __init__(
        self,
        config: Config,
        repository: DatabaseRepository,
        tmp_dir: Path
    ):
        """
        初始化
        
        Args:
            config: 配置对象
            repository: 数据库仓库对象
            tmp_dir: 临时下载目录
        """
        self.config = config
        self.repository = repository
        self.tmp_dir = Path(tmp_dir)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        
        # 获取 obsutil 可执行文件路径
        self.obsutil_exe = config.ingest.get('obsutil_exe') or 'obsutil'
    
    def import_all_ranges(self) -> Dict[str, any]:
        """
        导入所有preset的所有range文件
        
        Returns:
            导入统计信息
        """
        logger.info("=" * 80)
        logger.info("开始导入所有 Range 规则")
        logger.info("=" * 80)
        
        start_time = time.time()
        stats = {
            'total_presets': 0,
            'total_models': 0,
            'total_rules': 0,
            'success_rules': 0,
            'failed_rules': 0,
            'total_field_ranges': 0,
            'errors': []
        }
        
        # 获取所有presets
        presets = self.config.presets
        stats['total_presets'] = len(presets)
        
        for preset in presets:
            try:
                preset_stats = self._import_preset_ranges(preset)
                stats['total_models'] += preset_stats['total_models']
                stats['total_rules'] += preset_stats['total_rules']
                stats['success_rules'] += preset_stats['success_rules']
                stats['failed_rules'] += preset_stats['failed_rules']
                stats['total_field_ranges'] += preset_stats['total_field_ranges']
                stats['errors'].extend(preset_stats['errors'])
            except Exception as e:
                error_msg = f"导入preset失败 {preset['presets_id']}: {e}"
                logger.error(error_msg)
                stats['errors'].append(error_msg)
        
        elapsed_time = time.time() - start_time
        
        logger.info("=" * 80)
        logger.info("Range 导入完成")
        logger.info(f"处理 Presets: {stats['total_presets']}")
        logger.info(f"处理 Models: {stats['total_models']}")
        logger.info(f"处理 Rules: {stats['total_rules']} (成功: {stats['success_rules']}, 失败: {stats['failed_rules']})")
        logger.info(f"插入 Field Ranges: {stats['total_field_ranges']}")
        logger.info(f"总耗时: {elapsed_time:.2f}秒")
        
        if stats['errors']:
            logger.warning(f"遇到 {len(stats['errors'])} 个错误:")
            for error in stats['errors'][:10]:  # 只显示前10个
                logger.warning(f"  - {error}")
        
        logger.info("=" * 80)
        
        return stats
    
    def _import_preset_ranges(self, preset: Dict) -> Dict[str, any]:
        """
        导入单个preset的所有range
        
        Args:
            preset: preset配置
            
        Returns:
            导入统计信息
        """
        presets_id = preset['presets_id']
        logger.info(f"\n处理 Preset: {presets_id}")
        
        stats = {
            'total_models': 0,
            'total_rules': 0,
            'success_rules': 0,
            'failed_rules': 0,
            'total_field_ranges': 0,
            'errors': []
        }
        
        try:
            # 创建 OBS 客户端
            obs_client = ObsClient(
                config_path=preset['obsutil_config_path'],
                obsutil_exe=self.obsutil_exe
            )
            
            # 列举所有 range 文件
            logger.info(f"列举 OBS 文件: obs://{preset['rule_obs_bucket']}/{preset['rule_obs_path']}")
            all_files = obs_client.list_files(
                bucket=preset['rule_obs_bucket'],
                path=preset['rule_obs_path'],
                limit=self.config.ingest.get('obs_list_limit', 10000),
                max_total=self.config.ingest.get('max_total_files', 50000)
            )
            
            # 过滤出 range 文件（从返回的字典中提取 path）
            range_files = [f['path'] for f in all_files if 'range/' in f['path'] and f['path'].endswith('.csv')]
            logger.info(f"发现 {len(range_files)} 个 range CSV 文件")
            
            if not range_files:
                logger.warning(f"未发现任何 range 文件: {presets_id}")
                return stats
            
            # 按 model 和 rule_type 分组，选择最新的文件
            grouped_files = self._group_and_select_latest(range_files)
            stats['total_models'] = len(grouped_files)
            
            for (model, rule_type), obs_key in grouped_files.items():
                stats['total_rules'] += 1
                try:
                    # 导入单个规则
                    field_range_count = self._import_single_rule(
                        preset=preset,
                        model=model,
                        rule_type=rule_type,
                        obs_key=obs_key,
                        obs_client=obs_client
                    )
                    stats['success_rules'] += 1
                    stats['total_field_ranges'] += field_range_count
                except Exception as e:
                    error_msg = f"{presets_id}/{model}/{rule_type}: {e}"
                    logger.error(f"导入规则失败: {error_msg}")
                    stats['failed_rules'] += 1
                    stats['errors'].append(error_msg)
        
        except Exception as e:
            error_msg = f"处理preset失败 {presets_id}: {e}"
            logger.error(error_msg)
            stats['errors'].append(error_msg)
        
        return stats
    
    def _group_and_select_latest(
        self,
        range_files: List[str]
    ) -> Dict[Tuple[str, str], str]:
        """
        按 model 和 rule_type 分组，选择最新的文件
        
        Args:
            range_files: range文件路径列表
            
        Returns:
            (model, rule_type) -> 最新文件路径 的映射
        """
        # 按 (model, rule_type) 分组
        grouped = defaultdict(list)
        
        for obs_key in range_files:
            metadata = RangeParser.parse_range_path(obs_key)
            if metadata:
                key = (metadata['model'], metadata['rule_type'])
                grouped[key].append((obs_key, metadata['datetime']))
        
        # 每组选择最新的
        latest_files = {}
        for key, files in grouped.items():
            # 按时间降序排序，取第一个
            files.sort(key=lambda x: x[1], reverse=True)
            latest_files[key] = files[0][0]
            
            model, rule_type = key
            logger.info(f"  选择最新规则: {model}/{rule_type} -> {files[0][0]}")
        
        return latest_files
    
    def _import_single_rule(
        self,
        preset: Dict,
        model: str,
        rule_type: str,
        obs_key: str,
        obs_client: ObsClient
    ) -> int:
        """
        导入单个规则
        
        Args:
            preset: preset配置
            model: 机器人型号
            rule_type: 规则类型
            obs_key: OBS文件路径
            obs_client: OBS客户端
            
        Returns:
            插入的field_range数量
        """
        presets_id = preset['presets_id']
        logger.info(f"\n导入规则: {presets_id}/{model}/{rule_type}")
        logger.info(f"  文件: {obs_key}")
        
        # 解析路径元数据
        metadata = RangeParser.parse_range_path(obs_key)
        if not metadata:
            raise ValueError(f"无法解析路径: {obs_key}")
        
        # 下载文件
        local_path = self.tmp_dir / f"range_{model}_{rule_type}_{metadata['time_str']}.csv"
        logger.info(f"  下载到: {local_path}")
        
        # obs_key 已经是完整的 obs://bucket/path 格式
        obs_client.download_file(obs_key, local_path)
        
        if not local_path.exists():
            raise FileNotFoundError(f"下载失败: {local_path}")
        
        # 解析 CSV
        logger.info(f"  解析 CSV...")
        ranges = RangeParser.parse_range_csv(local_path)
        
        if not ranges:
            logger.warning(f"  CSV 为空或解析失败: {obs_key}")
            return 0
        
        logger.info(f"  解析到 {len(ranges)} 条字段范围")
        
        # 读取完整CSV内容
        with open(local_path, 'r', encoding='utf-8') as f:
            csv_content = f.read()
        
        # 清理临时文件
        if self.config.ingest.get('cleanup_tmp', False):
            local_path.unlink()
        
        # 获取或创建设备
        device_id = self.repository.get_or_create_device(
            presets_id=presets_id,
            sn='_default_',  # range规则不绑定特定设备，使用默认占位符
            model=model,
            area=preset['area']
        )
        
        # obs_key 已经包含完整路径
        obs_uri = obs_key
        
        # 创建或更新规则（会删除旧的field_range）
        logger.info(f"  创建/更新规则...")
        rule_id = self.repository.upsert_rule(
            presets_id=presets_id,
            device_id=device_id,
            rule_type=rule_type,
            rule_obs_bucket=preset['rule_obs_bucket'],
            rule_obs_path=obs_uri,
            rule_file_name=metadata['obs_key'].split('/')[-1],
            rule_file_time=metadata['datetime'],
            csv_rule_txt=csv_content
        )
        
        logger.info(f"  规则ID: {rule_id}")
        
        # 批量获取 field_id
        logger.info(f"  匹配字段...")
        field_keys = [r['field'] for r in ranges]
        field_id_map = self.repository.batch_get_field_ids(model, field_keys)
        
        # 准备 field_range 数据
        ranges_data = []
        skipped_count = 0
        
        for range_item in ranges:
            field_key = range_item['field']
            
            if field_key not in field_id_map:
                logger.warning(f"  跳过未知字段: {field_key}")
                skipped_count += 1
                continue
            
            ranges_data.append({
                'field_id': field_id_map[field_key],
                'rule_id': rule_id,
                'min_range': range_item['min'],
                'max_range': range_item['max']
            })
        
        if skipped_count > 0:
            logger.warning(f"  跳过 {skipped_count} 个未知字段")
        
        # 批量插入 field_range
        if ranges_data:
            logger.info(f"  插入 {len(ranges_data)} 条字段范围...")
            result = self.repository.batch_insert_field_ranges(ranges_data)
            logger.info(
                f"  ✓ 总数={result['total']}, "
                f"插入={result['inserted']}, "
                f"更新={result['updated']}, "
                f"跳过={result['skipped']}"
            )
            return result['inserted'] + result['updated']
        else:
            logger.warning(f"  无有效字段范围可插入")
            return 0
