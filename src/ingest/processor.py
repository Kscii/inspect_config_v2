"""
数据处理器模块
协调 OBS、数据库、元数据提取等模块，完成数据收集流程
支持并发下载、预过滤、批量插入
"""
import time
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from ..utils import logger
from ..config import Config
from ..database import (
    DatabaseRepository,
    get_connection_from_pool,
    return_connection_to_pool,
    init_connection_pool
)
from ..obs import ObsClient, PathParser
from .extractor import MetadataExtractor


class DataProcessor:
    """数据处理器类（支持并发处理）"""
    
    def __init__(
        self,
        config: Config,
        repository: DatabaseRepository,
        tmp_dir: Path
    ):
        """
        初始化数据处理器
        
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
        
        # 线程安全的统计锁
        self._stats_lock = threading.Lock()
    
    def sync_preset_to_db(self, preset: Dict) -> None:
        """
        同步 preset 配置到数据库
        
        Args:
            preset: preset 配置字典
        """
        self.repository.upsert_preset(preset)
    
    def process_preset(self, preset: Dict) -> Dict[str, int]:
        """
        处理单个 preset 的数据收集
        
        Args:
            preset: preset 配置字典
            
        Returns:
            统计信息字典，包含 listed, downloaded, inserted, errors, skipped
        """
        presets_id = preset['presets_id']
        logger.info(f"\n{'='*60}")
        logger.info(f"开始处理: {presets_id}")
        logger.info(f"{'='*60}")
        
        # 总体计时开始
        start_time_total = time.time()
        
        # 创建 ingest_run 记录
        run_id = self.repository.create_ingest_run(presets_id)
        logger.info(f"创建运行记录: run_id={run_id}")
        
        stats = {
            'listed': 0,
            'downloaded': 0,
            'inserted': 0,
            'errors': 0,
            'skipped': 0,
            'skipped_existing': 0,      # 预过滤：数据库已存在
            'skipped_parse_path': 0,    # 路径解析失败
            'skipped_download': 0,      # 下载失败
            'skipped_parse_json': 0,    # JSON解析失败
            'skipped_missing_fields': 0,  # 缺少必要字段
            'skipped_duplicate': 0      # 批量插入时重复
        }
        
        try:
            # 创建 OBS 客户端
            obs_client = ObsClient(
                config_path=preset['obsutil_config_path'],
                obsutil_exe=self.obsutil_exe
            )
            
            # 测试连接
            logger.info("测试 OBS 连接...")
            start_time = time.time()
            if not obs_client.test_connection(preset['report_obs_bucket']):
                error_msg = f"无法连接到 {preset['report_obs_bucket']}"
                logger.error(error_msg)
                self.repository.update_ingest_run(
                    run_id, success=False, error_message=error_msg
                )
                return stats
            
            # 列出所有文件（支持分页）
            logger.info("开始列举文件...")
            start_time = time.time()
            limit = self.config.ingest.get('obs_list_limit', 1000)
            max_total = self.config.ingest.get('max_total_files', 500000)
            files = obs_client.list_files(
                bucket=preset['report_obs_bucket'],
                path=preset['report_obs_path'],
                limit=limit,
                max_total=max_total
            )
            stats['listed'] = len(files)
            elapsed = time.time() - start_time
            logger.info(f"⏱️  列举文件完成: {len(files)} 个文件，耗时 {elapsed:.2f}秒 (平均 {elapsed/max(len(files),1)*1000:.1f}ms/文件)")
            
            if len(files) == 0:
                logger.warning(f"未找到任何文件: {preset['report_obs_bucket']}/{preset['report_obs_path']}")
                self.repository.update_ingest_run(
                    run_id,
                    listed_count=stats['listed'],
                    success=True
                )
                return stats
            
            # 根据 download_mode 进行预过滤
            download_mode = self.config.ingest.get('download_mode', 'new_only')
            logger.info(f"下载模式: {download_mode}")
            
            if download_mode == "new_only":
                # 模式3：只下载数据库中不存在的 episode
                logger.info("开始预过滤已存在文件...")
                start_time = time.time()
                files, skipped_existing = self._filter_existing_files(files)
                stats['skipped_existing'] = skipped_existing
                stats['skipped'] += skipped_existing
                elapsed = time.time() - start_time
                logger.info(f"⏱️  预过滤完成: 剩余 {len(files)} 个文件需要处理，已存在 {skipped_existing} 个，耗时 {elapsed:.2f}秒")
                
            elif download_mode == "incremental":
                # 模式2：只下载 LastModified 晚于上次成功运行的文件
                logger.info("开始增量过滤...")
                start_time = time.time()
                files, skipped_old = self._filter_by_last_modified(files, presets_id)
                stats['skipped_existing'] = skipped_old
                stats['skipped'] += skipped_old
                elapsed = time.time() - start_time
                logger.info(f"⏱️  增量过滤完成: 剩余 {len(files)} 个文件需要处理，过滤掉 {skipped_old} 个旧文件，耗时 {elapsed:.2f}秒")
                
            elif download_mode == "full":
                # 模式1：全量下载，不过滤
                logger.info("全量下载模式，不进行预过滤")
            
            # 处理文件
            logger.info("开始处理文件...")
            start_time = time.time()
            stats = self._process_files_concurrent(
                files, obs_client, preset, stats, download_mode
            )
            elapsed = time.time() - start_time
            logger.info(f"⏱️  文件处理完成: 耗时 {elapsed:.2f}秒，平均 {elapsed/max(len(files),1):.2f}秒/文件")
            
            # 更新运行记录（成功）
            self.repository.update_ingest_run(
                run_id=run_id,
                listed_count=stats['listed'],
                downloaded_count=stats['downloaded'],
                inserted_count=stats['inserted'],
                success=True
            )
            
            total_elapsed = time.time() - start_time_total
            logger.info(f"完成 {presets_id}: "
                       f"列出 {stats['listed']}, "
                       f"下载 {stats['downloaded']}, "
                       f"插入 {stats['inserted']}, "
                       f"跳过 {stats['skipped']}, "
                       f"错误 {stats['errors']}")
            
            # 输出详细的跳过分类统计
            if stats['skipped'] > 0:
                logger.info(f"跳过详情: "
                           f"已存在={stats['skipped_existing']}, "
                           f"路径解析失败={stats['skipped_parse_path']}, "
                           f"下载失败={stats['skipped_download']}, "
                           f"JSON解析失败={stats['skipped_parse_json']}, "
                           f"缺少字段={stats['skipped_missing_fields']}, "
                           f"重复={stats['skipped_duplicate']}")
            logger.info(f"⏱️  总耗时: {total_elapsed:.2f}秒 ({total_elapsed/60:.1f}分钟)")
            
            return stats
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"处理 preset 失败 {presets_id}: {error_msg}")
            
            # 更新运行记录（失败）
            self.repository.update_ingest_run(
                run_id=run_id,
                listed_count=stats['listed'],
                downloaded_count=stats['downloaded'],
                inserted_count=stats['inserted'],
                success=False,
                error_message=error_msg
            )
            
            return stats
    
    def _filter_existing_files(self, files: List[Dict]) -> Tuple[List[Dict], int]:
        """
        预过滤已存在的文件（用于 new_only 模式）
        
        Args:
            files: 所有文件列表（包含 path 和 last_modified）
            
        Returns:
            (需要处理的文件列表, 跳过的数量)
        """
        # 解析所有 episode_id
        episode_ids = []
        file_map = {}  # episode_id -> file_info
        
        for file_info in files:
            obs_path = file_info['path']
            path_info = PathParser.parse(obs_path)
            if path_info:
                episode_id = path_info['episode_id']
                episode_ids.append(episode_id)
                file_map[episode_id] = file_info
        
        # 批量查询已存在的 episode
        existing = self.repository.get_existing_episodes(episode_ids)
        
        # 过滤掉已存在的
        filtered_files = [
            file_map[episode_id]
            for episode_id in episode_ids
            if episode_id not in existing
        ]
        
        skipped_count = len(existing)
        logger.info(f"预过滤: {len(files)} 个文件，{skipped_count} 个已存在，{len(filtered_files)} 个需要处理")
        
        return filtered_files, skipped_count
    
    def _filter_by_last_modified(self, files: List[Dict], presets_id: str) -> Tuple[List[Dict], int]:
        """
        基于 LastModified 时间的增量过滤（用于 incremental 模式）
        
        只保留 LastModified > 上次成功运行的 started_at 的文件
        如果没有成功运行记录，则回退到 full 模式（不过滤）
        
        Args:
            files: 所有文件列表（包含 path 和 last_modified）
            presets_id: preset ID
            
        Returns:
            (需要处理的文件列表, 跳过的数量)
        """
        # 查询最近一次成功的运行
        last_run = self.repository.get_last_successful_run(presets_id)
        
        if not last_run:
            logger.warning(f"未找到 {presets_id} 的成功运行记录，回退到 full 模式（不过滤）")
            return files, 0
        
        last_started_at = last_run['started_at']
        logger.info(f"上次成功运行时间: {last_started_at}")
        
        # 过滤：只保留 last_modified > last_started_at 的文件
        filtered_files = []
        for file_info in files:
            last_modified_str = file_info['last_modified']
            # 解析 ISO 8601 时间字符串
            try:
                # 去掉 'Z' 并解析
                if last_modified_str.endswith('Z'):
                    last_modified_str = last_modified_str[:-1] + '+00:00'
                last_modified = datetime.fromisoformat(last_modified_str)
                
                # 比较时间（确保都是 timezone-aware）
                if last_modified.tzinfo is None:
                    # 如果没有时区信息，假设为 UTC
                    from datetime import timezone
                    last_modified = last_modified.replace(tzinfo=timezone.utc)
                
                if last_modified > last_started_at:
                    filtered_files.append(file_info)
            except Exception as e:
                logger.warning(f"解析时间失败 {file_info['path']}: {e}，保留该文件")
                filtered_files.append(file_info)
        
        skipped_count = len(files) - len(filtered_files)
        logger.info(f"增量过滤: {len(files)} 个文件，{skipped_count} 个太旧，{len(filtered_files)} 个需要处理")
        
        return filtered_files, skipped_count
    
    def _process_files_concurrent(
        self,
        files: List[Dict],
        obs_client: ObsClient,
        preset: Dict,
        stats: Dict,
        download_mode: str
    ) -> Dict[str, int]:
        """
        并发处理文件
        
        Args:
            files: 文件列表（包含 path 和 last_modified）
            obs_client: OBS 客户端
            preset: preset 配置
            stats: 统计信息
            download_mode: 下载模式
            
        Returns:
            更新后的统计信息
        """
        workers = self.config.ingest.get('concurrent_workers', 16)
        batch_size = self.config.ingest.get('batch_insert_size', 100)
        
        logger.info(f"使用并发处理: {workers} 个线程")
        
        # 初始化连接池
        init_connection_pool(self.config.database, min_conn=2, max_conn=workers + 5)
        
        # 批量数据缓冲区
        batch_buffer = []
        buffer_lock = threading.Lock()
        
        def process_one_file(file_info: Dict) -> Optional[Dict]:
            """处理单个文件，返回 (result, skip_reason)"""
            obs_path = file_info['path']
            try:
                # 从连接池获取连接
                conn = get_connection_from_pool(self.config.database)
                repo = DatabaseRepository(conn)
                
                try:
                    result, skip_reason = self._process_single_file(
                        obs_client, obs_path, preset, repo
                    )
                    return result, skip_reason
                finally:
                    # 归还连接到池
                    return_connection_to_pool(conn)
                    
            except Exception as e:
                logger.error(f"处理文件失败 {obs_path}: {e}")
                with self._stats_lock:
                    stats['errors'] += 1
                return None, 'error'
        
        def flush_batch():
            """提交批量数据"""
            nonlocal batch_buffer
            if batch_buffer:
                try:
                    batch_start = time.time()
                    result = self.repository.batch_insert_episodes(batch_buffer, download_mode)
                    batch_elapsed = time.time() - batch_start
                    with self._stats_lock:
                        stats['inserted'] += result['inserted_episodes']
                        if download_mode in ("incremental", "full"):
                            # 增量和全量模式下，记录更新数量
                            stats['inserted'] += result['updated_episodes']
                        stats['skipped'] += result['skipped_episodes']
                        stats['skipped_duplicate'] += result['skipped_episodes']
                    
                    if download_mode == "new_only":
                        logger.info(
                            f"⏱️  批量提交 {result['total']} 条数据，"
                            f"插入 {result['inserted_episodes']}，"
                            f"重复跳过 {result['skipped_episodes']}，"
                            f"耗时 {batch_elapsed:.2f}秒"
                        )
                    else:
                        logger.info(
                            f"⏱️  批量提交 {result['total']} 条数据，"
                            f"插入 {result['inserted_episodes']}，"
                            f"更新 {result['updated_episodes']}，"
                            f"重复跳过 {result['skipped_episodes']}，"
                            f"耗时 {batch_elapsed:.2f}秒"
                        )
                    batch_buffer = []
                except Exception as e:
                    logger.error(f"批量提交失败: {e}")
                    with self._stats_lock:
                        stats['errors'] += len(batch_buffer)
                    batch_buffer = []
        
        # 使用线程池并发处理
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(process_one_file, f): f for f in files}
            
            for future in as_completed(futures):
                result, skip_reason = future.result()
                
                if result:
                    with self._stats_lock:
                        stats['downloaded'] += 1
                    
                    # 添加到批量缓冲区
                    with buffer_lock:
                        batch_buffer.append(result)
                        
                        # 达到批量大小时提交
                        if len(batch_buffer) >= batch_size:
                            flush_batch()
                else:
                    with self._stats_lock:
                        # 根据跳过原因更新统计
                        if skip_reason == 'parse_path':
                            stats['skipped_parse_path'] += 1
                        elif skip_reason == 'download':
                            stats['skipped_download'] += 1
                        elif skip_reason == 'parse_json':
                            stats['skipped_parse_json'] += 1
                        elif skip_reason == 'missing_fields':
                            stats['skipped_missing_fields'] += 1
        
        # 提交剩余数据
        with buffer_lock:
            flush_batch()
        
        return stats
    
    def _process_single_file(
        self,
        obs_client: ObsClient,
        obs_path: str,
        preset: Dict,
        repo: DatabaseRepository
    ) -> Tuple[Optional[Dict], Optional[str]]:
        """
        处理单个文件
        
        Args:
            obs_client: OBS 客户端
            obs_path: OBS 文件路径
            preset: preset 配置
            repo: 数据库仓库
            
        Returns:
            Tuple[处理后的数据字典或None, 跳过原因或None]
            - (data_dict, None): 处理成功
            - (None, 'parse_path'): 路径解析失败
            - (None, 'download'): 下载失败
            - (None, 'parse_json'): JSON解析失败
            - (None, 'missing_fields'): 缺少必要字段
        """
        step_times = {}  # 记录各步骤耗时
        file_start = time.time()
        
        # 解析路径
        step_start = time.time()
        path_info = PathParser.parse(obs_path)
        if not path_info:
            logger.warning(f"无法解析路径，跳过: {obs_path}")
            return None, 'parse_path'
        
        task_id = path_info['task_id']
        episode_id = path_info['episode_id']
        step_times['parse_path'] = time.time() - step_start
        
        # 下载 JSON
        step_start = time.time()
        filename = obs_path.split('/')[-1]
        local_path = self.tmp_dir / filename
        
        if not obs_client.download_file(obs_path, local_path):
            logger.error(f"下载失败，跳过: {obs_path}")
            return None, 'download'
        step_times['download'] = time.time() - step_start
        
        # 解析 JSON
        step_start = time.time()
        report_json = MetadataExtractor.load_json(local_path)
        if not report_json:
            logger.error(f"解析 JSON 失败，跳过: {local_path}")
            return None, 'parse_json'
        step_times['load_json'] = time.time() - step_start
        
        # 提取元数据
        step_start = time.time()
        metadata = MetadataExtractor.extract_metadata(report_json)
        step_times['extract_metadata'] = time.time() - step_start
        
        # 验证必要字段
        if not metadata.get('sn') or not metadata.get('model'):
            logger.warning(f"缺少 sn 或 model，跳过: {episode_id}")
            return None, 'missing_fields'
        
        # 获取 api_map_id (从 config 的 api_map_ids 中查找：通过 model 反查 id)
        api_map_id = None
        if hasattr(self.config, 'api_map_ids'):
            # api_map_ids 格式: {presets_id: {id: model}}
            # 需要反向查找: 通过 model 找到对应的 id
            id_to_model = self.config.api_map_ids.get(preset['presets_id'], {})
            for map_id, model_name in id_to_model.items():
                if model_name == metadata['model']:
                    api_map_id = str(map_id)
                    break
        
        # 创建或获取设备
        step_start = time.time()
        device_id = repo.get_or_create_device(
            presets_id=preset['presets_id'],
            sn=metadata['sn'],
            model=metadata['model'],
            area=preset['area'],
            api_map_id=api_map_id
        )
        step_times['get_or_create_device'] = time.time() - step_start
        
        # 创建或获取任务
        step_start = time.time()
        repo.get_or_create_task(
            task_id=task_id,
            presets_id=preset['presets_id'],
            device_id=device_id
        )
        step_times['get_or_create_task'] = time.time() - step_start
        
        # 解析时间：collect_at 使用 report.createdAt
        collect_at = MetadataExtractor.parse_datetime(metadata.get('created_at'))
        report_created_at = MetadataExtractor.parse_datetime(metadata.get('created_at'))
        
        # 清理临时文件（如果配置需要）
        if self.config.ingest.get('cleanup_tmp', False):
            local_path.unlink(missing_ok=True)
        
        # 计算总耗时并输出详细计时（仅在DEBUG模式）
        total_time = time.time() - file_start
        if logger.level <= 10:  # DEBUG level
            timing_details = ', '.join([f"{k}={v*1000:.1f}ms" for k, v in step_times.items()])
            logger.debug(f"📊 处理 {episode_id}: 总耗时 {total_time*1000:.1f}ms ({timing_details})")
        
        # 返回数据供批量插入（成功情况下skip_reason为None）
        return ({
            'episode_id': episode_id,
            'task_id': task_id,
            'device_id': device_id,
            'collect_at': collect_at,
            'report_json': report_json,
            'obs_path': obs_path,
            'json_ver': metadata.get('json_ver', 'unknown'),
            'report_created_at': report_created_at
        }, None)
