"""
数据库操作仓库模块
封装所有数据库 CRUD 操作
"""
import json
from io import StringIO
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import psycopg2
from psycopg2.extras import execute_values
from ..utils import logger


class DatabaseRepository:
    """数据库操作仓库类"""
    
    def __init__(self, connection: psycopg2.extensions.connection):
        """
        初始化
        
        Args:
            connection: 数据库连接对象
        """
        self.conn = connection
    
    def get_presets_from_db(self) -> List[Dict]:
        """
        从数据库获取所有 presets 配置
        
        Returns:
            presets 配置列表
        """
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                SELECT presets_id, area, environment, 
                       report_obs_bucket, report_obs_path,
                       rule_obs_bucket, rule_obs_path,
                       obsutil_config_path
                FROM presets
                ORDER BY area, environment
            """)
            
            presets = []
            for row in cursor.fetchall():
                presets.append({
                    'presets_id': row[0],
                    'area': row[1],
                    'environment': row[2],
                    'report_obs_bucket': row[3],
                    'report_obs_path': row[4],
                    'rule_obs_bucket': row[5],
                    'rule_obs_path': row[6],
                    'obsutil_config_path': row[7]
                })
            
            logger.info(f"从数据库获取到 {len(presets)} 个 presets 配置")
            return presets
        finally:
            cursor.close()
    
    def upsert_preset(self, preset: Dict) -> None:
        """
        插入或更新 preset 配置到数据库
        
        Args:
            preset: preset 配置字典
        """
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO presets (
                    presets_id, area, environment,
                    report_obs_bucket, report_obs_path,
                    rule_obs_bucket, rule_obs_path,
                    obsutil_config_path
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (presets_id) DO UPDATE SET
                    area = EXCLUDED.area,
                    environment = EXCLUDED.environment,
                    report_obs_bucket = EXCLUDED.report_obs_bucket,
                    report_obs_path = EXCLUDED.report_obs_path,
                    rule_obs_bucket = EXCLUDED.rule_obs_bucket,
                    rule_obs_path = EXCLUDED.rule_obs_path,
                    obsutil_config_path = EXCLUDED.obsutil_config_path,
                    updated_at = now()
            """, (
                preset['presets_id'],
                preset['area'],
                preset['environment'],
                preset['report_obs_bucket'],
                preset['report_obs_path'],
                preset['rule_obs_bucket'],
                preset['rule_obs_path'],
                preset['obsutil_config_path']
            ))
            self.conn.commit()
            logger.info(f"已同步 preset 到数据库: {preset['presets_id']}")
        except Exception as e:
            self.conn.rollback()
            logger.error(f"同步 preset 失败: {e}")
            raise
        finally:
            cursor.close()
    
    def get_or_create_device(self, presets_id: str, sn: str, model: str, area: str, api_map_id: str = None) -> str:
        """
        获取或创建设备记录（并发安全）
        
        Args:
            presets_id: preset ID
            sn: 设备序列号
            model: 设备型号
            area: 区域
            api_map_id: API映射ID（同preset_id和model共享）
            
        Returns:
            device_id (UUID string)
        """
        cursor = self.conn.cursor()
        try:
            # 使用 INSERT ... ON CONFLICT 确保并发安全
            # 如果已存在则不插入，直接返回已有记录
            cursor.execute("""
                INSERT INTO device (presets_id, sn, area, model, api_map_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (presets_id, model, sn) DO NOTHING
                RETURNING device_id
            """, (presets_id, sn, area, model, api_map_id))
            
            row = cursor.fetchone()
            if row:
                # 新创建的设备
                device_id = str(row[0])
                self.conn.commit()
                logger.info(f"创建新设备: {model}/{sn} -> {device_id}")
                return device_id
            
            # 设备已存在，查询返回
            cursor.execute("""
                SELECT device_id FROM device
                WHERE presets_id = %s AND model = %s AND sn = %s
            """, (presets_id, model, sn))
            
            device_id = str(cursor.fetchone()[0])
            self.conn.commit()
            return device_id
        except Exception as e:
            self.conn.rollback()
            logger.error(f"获取或创建设备失败: {e}")
            raise
        finally:
            cursor.close()
    
    def get_or_create_task(self, task_id: str, presets_id: str, device_id: str) -> str:
        """
        获取或创建任务记录（并发安全）
        
        Args:
            task_id: 任务 ID
            presets_id: preset ID
            device_id: 设备 ID
            
        Returns:
            task_id
        """
        cursor = self.conn.cursor()
        try:
            # 使用 INSERT ... ON CONFLICT 确保并发安全
            # task_id 是主键，冲突时不做任何操作
            cursor.execute("""
                INSERT INTO task (task_id, presets_id, device_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (task_id) DO NOTHING
            """, (task_id, presets_id, device_id))
            
            self.conn.commit()
            
            # 如果有影响行数说明是新创建的
            if cursor.rowcount > 0:
                logger.info(f"创建新任务: {task_id}")
            
            return task_id
        except Exception as e:
            self.conn.rollback()
            logger.error(f"获取或创建任务失败: {e}")
            raise
        finally:
            cursor.close()
    
    def episode_exists(self, episode_id: str) -> bool:
        """
        检查 episode 是否已存在
        
        Args:
            episode_id: episode ID
            
        Returns:
            是否存在
        """
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT 1 FROM episode WHERE episode_id = %s", (episode_id,))
            return cursor.fetchone() is not None
        finally:
            cursor.close()
    
    def insert_episode(
        self,
        episode_id: str,
        task_id: str,
        device_id: str,
        collect_at: datetime,
        report_json: Dict,
        obs_path: str,
        json_ver: str,
        report_created_at: datetime
    ) -> None:
        """
        插入 episode 及相关数据
        
        Args:
            episode_id: episode ID
            task_id: 任务 ID
            device_id: 设备 ID
            collect_at: 采集时间（report.createdAt）
            report_json: 完整报告 JSON
            obs_path: OBS 文件路径
            json_ver: JSON 版本
            report_created_at: json_report创建时间（report.createdAt）
        """
        cursor = self.conn.cursor()
        try:
            # 插入 episode（created_at 自动生成）
            cursor.execute("""
                INSERT INTO episode (episode_id, task_id, device_id, collect_at)
                VALUES (%s, %s, %s, %s)
            """, (episode_id, task_id, device_id, collect_at))
            
            # 插入 json_report（created_at 使用 report.createdAt）
            cursor.execute("""
                INSERT INTO json_report (
                    episode_id, file_name, json_report_ver, 
                    json_report, json_report_download_path, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                episode_id,
                obs_path.split('/')[-1],
                json_ver,
                json.dumps(report_json, ensure_ascii=False),
                obs_path,
                report_created_at
            ))
            
            self.conn.commit()
            logger.debug(f"插入 episode: {episode_id}")
        except Exception as e:
            self.conn.rollback()
            logger.error(f"插入 episode 失败 {episode_id}: {e}")
            raise
        finally:
            cursor.close()
    
    def reset_all_episode_update_flags(self) -> int:
        """
        全局初始化：将所有 episode 的 is_updated 标记重置为 false
        在数据收集开始前调用
        
        Returns:
            重置的记录数
        """
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                UPDATE episode 
                SET is_updated = false
                WHERE is_updated = true
            """)
            count = cursor.rowcount
            self.conn.commit()
            logger.info(f"重置 {count} 个 episode 的 is_updated 标记")
            return count
        except Exception as e:
            self.conn.rollback()
            logger.error(f"重置 is_updated 标记失败: {e}")
            raise
        finally:
            cursor.close()
    
    def get_last_successful_run(self, presets_id: str) -> Optional[Dict]:
        """
        获取指定 preset 最近一次成功的运行记录
        
        Args:
            presets_id: preset ID
            
        Returns:
            运行记录字典，包含 run_id, started_at 等，如果没有则返回 None
        """
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                SELECT run_id, presets_id, started_at, finished_at,
                       listed_count, downloaded_count, inserted_episode_count
                FROM ingest_run
                WHERE presets_id = %s AND success = true
                ORDER BY started_at DESC
                LIMIT 1
            """, (presets_id,))
            
            row = cursor.fetchone()
            if row:
                return {
                    'run_id': row[0],
                    'presets_id': row[1],
                    'started_at': row[2],
                    'finished_at': row[3],
                    'listed_count': row[4],
                    'downloaded_count': row[5],
                    'inserted_count': row[6]
                }
            return None
        finally:
            cursor.close()
    
    def create_ingest_run(self, presets_id: str) -> int:
        """
        创建 ingest_run 记录
        
        Args:
            presets_id: preset ID
            
        Returns:
            run_id
        """
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO ingest_run (presets_id)
                VALUES (%s)
                RETURNING run_id
            """, (presets_id,))
            run_id = cursor.fetchone()[0]
            self.conn.commit()
            return run_id
        except Exception as e:
            self.conn.rollback()
            logger.error(f"创建 ingest_run 失败: {e}")
            raise
        finally:
            cursor.close()
    
    def update_ingest_run(
        self,
        run_id: int,
        listed_count: int = 0,
        downloaded_count: int = 0,
        inserted_count: int = 0,
        success: bool = True,
        error_message: Optional[str] = None
    ) -> None:
        """
        更新 ingest_run 记录
        
        Args:
            run_id: run ID
            listed_count: 列出的文件数
            downloaded_count: 下载的文件数
            inserted_count: 插入的 episode 数
            success: 是否成功
            error_message: 错误信息
        """
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                UPDATE ingest_run
                SET listed_count = %s,
                    downloaded_count = %s,
                    inserted_episode_count = %s,
                    finished_at = now(),
                    success = %s,
                    error_message = %s
                WHERE run_id = %s
            """, (listed_count, downloaded_count, inserted_count, success, error_message, run_id))
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            logger.error(f"更新 ingest_run 失败: {e}")
            raise
        finally:
            cursor.close()
    
    # ===== 性能优化方法 =====
    
    def get_existing_episodes(self, episode_ids: List[str]) -> set:
        """
        批量检查哪些 episode_id 已存在
        
        Args:
            episode_ids: episode ID 列表
            
        Returns:
            已存在的 episode_id 集合
        """
        if not episode_ids:
            return set()
        
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                SELECT episode_id FROM episode 
                WHERE episode_id = ANY(%s)
            """, (episode_ids,))
            
            existing = {row[0] for row in cursor.fetchall()}
            logger.info(f"检查 {len(episode_ids)} 个 episode，{len(existing)} 个已存在")
            return existing
        finally:
            cursor.close()
    
    def batch_insert_episodes(self, episodes_data: List[Dict], download_mode: str = "new_only") -> Dict[str, int]:
        """
        批量插入 episode 数据（使用临时表 + COPY + 根据模式处理）
        
        策略：
        1. 创建临时表 (ON COMMIT DROP)
        2. COPY 数据到临时表 (高性能)
        3. 根据 download_mode 处理插入/更新逻辑
        4. 标记 is_updated = true
        
        Args:
            episodes_data: episode 数据列表，每项包含：
                - episode_id, task_id, device_id, collect_at
                - report_json, obs_path, json_ver, report_created_at
            download_mode: 下载模式
                - "new_only": 只插入新的，冲突时不处理
                - "incremental": 更新已存在的
                - "full": 全量覆盖
                
        Returns:
            统计信息字典：
            {
                'total': 传入总数,
                'inserted_episodes': 实际插入的episode数,
                'updated_episodes': 实际更新的episode数,
                'inserted_json_reports': 实际插入的json_report数,
                'skipped_episodes': 跳过的重复episode数
            }
        """
        if not episodes_data:
            return {
                'total': 0,
                'inserted_episodes': 0,
                'updated_episodes': 0,
                'inserted_json_reports': 0,
                'skipped_episodes': 0
            }
        
        cursor = self.conn.cursor()
        try:
            # 1. 创建临时表（ON COMMIT DROP - 事务结束自动删除）
            cursor.execute("""
                CREATE TEMP TABLE tmp_episode (
                    episode_id text,
                    task_id text,
                    device_id uuid,
                    collect_at timestamptz
                ) ON COMMIT DROP
            """)
            
            cursor.execute("""
                CREATE TEMP TABLE tmp_json_report (
                    episode_id text,
                    file_name text,
                    json_report_ver text,
                    json_report jsonb,
                    json_report_download_path text,
                    created_at timestamptz
                ) ON COMMIT DROP
            """)
            
            # 2. COPY 数据到临时表
            # 准备 episode 数据
            episode_buffer = StringIO()
            for item in episodes_data:
                collect_at = item['collect_at'].isoformat() if item['collect_at'] else '\\N'
                row = f"{item['episode_id']}\t{item['task_id']}\t{item['device_id']}\t{collect_at}\n"
                episode_buffer.write(row)
            
            episode_buffer.seek(0)
            cursor.copy_expert(
                """
                COPY tmp_episode (episode_id, task_id, device_id, collect_at)
                FROM STDIN
                WITH (FORMAT text, DELIMITER E'\\t', NULL '\\N')
                """,
                episode_buffer
            )
            
            # 准备 json_report 数据
            json_report_buffer = StringIO()
            for item in episodes_data:
                file_name = item['obs_path'].split('/')[-1]
                json_str = json.dumps(item['report_json'], ensure_ascii=False).replace('\\', '\\\\').replace('\n', '\\n').replace('\t', '\\t')
                created_at = item['report_created_at'].isoformat() if item['report_created_at'] else '\\N'
                
                row = f"{item['episode_id']}\t{file_name}\t{item['json_ver']}\t{json_str}\t{item['obs_path']}\t{created_at}\n"
                json_report_buffer.write(row)
            
            json_report_buffer.seek(0)
            cursor.copy_expert(
                """
                COPY tmp_json_report (episode_id, file_name, json_report_ver, json_report, json_report_download_path, created_at)
                FROM STDIN
                WITH (FORMAT text, DELIMITER E'\\t', NULL '\\N')
                """,
                json_report_buffer
            )
            
            # 3. 根据 download_mode 执行不同的插入/更新策略
            inserted_episode_count = 0
            updated_episode_count = 0
            inserted_json_report_count = 0
            
            if download_mode == "new_only":
                # 模式1：只插入新的，冲突时不处理
                cursor.execute("""
                    WITH inserted AS (
                        INSERT INTO episode (episode_id, task_id, device_id, collect_at, is_updated)
                        SELECT episode_id, task_id, device_id, collect_at, true
                        FROM tmp_episode
                        ON CONFLICT (episode_id) DO NOTHING
                        RETURNING episode_id
                    )
                    SELECT COUNT(*) FROM inserted
                """)
                inserted_episode_count = cursor.fetchone()[0]
                
                # 只插入新 episode 对应的 json_report
                cursor.execute("""
                    WITH inserted AS (
                        INSERT INTO json_report (episode_id, file_name, json_report_ver, json_report, json_report_download_path, created_at)
                        SELECT jr.episode_id, jr.file_name, jr.json_report_ver, jr.json_report, jr.json_report_download_path, jr.created_at
                        FROM tmp_json_report jr
                        INNER JOIN episode e ON jr.episode_id = e.episode_id
                        ON CONFLICT (episode_id) DO NOTHING
                        RETURNING episode_id
                    )
                    SELECT COUNT(*) FROM inserted
                """)
                inserted_json_report_count = cursor.fetchone()[0]
                
            elif download_mode in ("incremental", "full"):
                # 模式2/3：插入新的或更新已存在的
                
                # 先删除将要被覆盖的 json_report（CASCADE 不影响 episode）
                cursor.execute("""
                    DELETE FROM json_report
                    WHERE episode_id IN (
                        SELECT episode_id FROM tmp_episode
                    )
                """)
                
                # 插入或更新 episode（并标记 is_updated = true）
                cursor.execute("""
                    WITH upserted AS (
                        INSERT INTO episode (episode_id, task_id, device_id, collect_at, is_updated)
                        SELECT episode_id, task_id, device_id, collect_at, true
                        FROM tmp_episode
                        ON CONFLICT (episode_id) DO UPDATE SET
                            task_id = EXCLUDED.task_id,
                            device_id = EXCLUDED.device_id,
                            collect_at = EXCLUDED.collect_at,
                            download_at = now(),
                            is_updated = true,
                            updated_at = now()
                        RETURNING episode_id, (xmax = 0) AS inserted
                    )
                    SELECT 
                        COUNT(*) FILTER (WHERE inserted) AS inserted_count,
                        COUNT(*) FILTER (WHERE NOT inserted) AS updated_count
                    FROM upserted
                """)
                result = cursor.fetchone()
                inserted_episode_count = result[0] or 0
                updated_episode_count = result[1] or 0
                
                # 插入新的 json_report（已删除旧的，所以不会冲突）
                cursor.execute("""
                    INSERT INTO json_report (episode_id, file_name, json_report_ver, json_report, json_report_download_path, created_at)
                    SELECT jr.episode_id, jr.file_name, jr.json_report_ver, jr.json_report, jr.json_report_download_path, jr.created_at
                    FROM tmp_json_report jr
                    INNER JOIN episode e ON jr.episode_id = e.episode_id
                    RETURNING episode_id
                """)
                inserted_json_report_count = cursor.rowcount
            
            # 5. 提交事务（临时表自动 DROP）
            self.conn.commit()
            
            # 6. 统计信息
            total_count = len(episodes_data)
            skipped_count = total_count - inserted_episode_count - updated_episode_count
            
            stats = {
                'total': total_count,
                'inserted_episodes': inserted_episode_count,
                'updated_episodes': updated_episode_count,
                'inserted_json_reports': inserted_json_report_count,
                'skipped_episodes': skipped_count
            }
            
            if download_mode == "new_only":
                logger.info(
                    f"批量插入 episode (new_only): 总数={total_count}, "
                    f"插入={inserted_episode_count}, 跳过={skipped_count}"
                )
            else:
                logger.info(
                    f"批量插入 episode ({download_mode}): 总数={total_count}, "
                    f"插入={inserted_episode_count}, 更新={updated_episode_count}, 跳过={skipped_count}"
                )
            
            return stats
            
        except Exception as e:
            self.conn.rollback()
            logger.error(f"批量插入失败: {e}")
            raise
        finally:
            cursor.close()
    
    # ===== 字段管理方法 =====
    
    def get_or_create_field(
        self,
        field_key: str,
        field_name: str,
        rule_code: str,
        field_type: str,
        data_type: str,
        model: str,
        is_selected: bool = True
    ) -> int:
        """
        获取或创建字段记录（并发安全）
        
        Args:
            field_key: 字段唯一键（selector）
            field_name: 字段名称
            rule_code: 规则代码
            field_type: 字段类型
            data_type: 数据类型（numeric/non_numeric）
            model: 机器人型号
            is_selected: 是否选中
            
        Returns:
            field_id
        """
        cursor = self.conn.cursor()
        try:
            # 使用 INSERT ... ON CONFLICT 确保并发安全
            cursor.execute("""
                INSERT INTO field (field_key, field_name, rule_code, field_type, data_type, model, is_selected)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (model, field_key) DO NOTHING
                RETURNING field_id
            """, (field_key, field_name, rule_code, field_type, data_type, model, is_selected))
            
            row = cursor.fetchone()
            if row:
                # 新创建的字段
                field_id = row[0]
                self.conn.commit()
                logger.debug(f"创建新字段: {model}/{field_key} -> field_id={field_id}")
                return field_id
            
            # 字段已存在，查询返回
            cursor.execute("""
                SELECT field_id FROM field
                WHERE model = %s AND field_key = %s
            """, (model, field_key))
            
            field_id = cursor.fetchone()[0]
            self.conn.commit()
            return field_id
        except Exception as e:
            self.conn.rollback()
            logger.error(f"获取或创建字段失败: {e}")
            raise
        finally:
            cursor.close()
    
    def batch_insert_fields(self, fields_data: List[Dict]) -> Dict[str, int]:
        """
        批量插入字段定义（使用 COPY 优化）
        
        Args:
            fields_data: 字段数据列表，每项包含：
                - field_key, field_name, rule_code, field_type, data_type, model, is_selected
                
        Returns:
            field_key -> field_id 的映射字典
        """
        if not fields_data:
            return {}
        
        cursor = self.conn.cursor()
        try:
            # 使用 COPY 批量插入
            buffer = StringIO()
            for item in fields_data:
                is_selected = 'true' if item.get('is_selected', True) else 'false'
                row = f"{item['field_key']}\t{item['field_name']}\t{item['rule_code']}\t{item['field_type']}\t{item['data_type']}\t{item['model']}\t{is_selected}\n"
                buffer.write(row)
            
            buffer.seek(0)
            cursor.copy_expert(
                """
                COPY field (field_key, field_name, rule_code, field_type, data_type, model, is_selected)
                FROM STDIN
                WITH (FORMAT text, DELIMITER E'\\t')
                """,
                buffer
            )
            
            self.conn.commit()
            logger.info(f"批量插入 {len(fields_data)} 个字段定义")
            
            # 查询所有 field_id
            model = fields_data[0]['model']  # 假设同一批次都是同一个 model
            field_keys = [item['field_key'] for item in fields_data]
            
            cursor.execute("""
                SELECT field_key, field_id FROM field
                WHERE model = %s AND field_key = ANY(%s)
            """, (model, field_keys))
            
            field_id_map = {row[0]: row[1] for row in cursor.fetchall()}
            return field_id_map
            
        except Exception as e:
            self.conn.rollback()
            logger.error(f"批量插入字段失败: {e}")
            raise
        finally:
            cursor.close()
    
    def batch_insert_field_values_num(self, values_data: List[Dict]) -> int:
        """
        批量插入数值型字段值（使用 COPY 优化）
        
        Args:
            values_data: 字段值数据列表，每项包含：
                - episode_id, field_id, value
                
        Returns:
            成功插入的数量
        """
        if not values_data:
            return 0
        
        cursor = self.conn.cursor()
        try:
            # 使用 COPY 批量插入
            buffer = StringIO()
            for item in values_data:
                row = f"{item['episode_id']}\t{item['field_id']}\t{item['value']}\n"
                buffer.write(row)
            
            buffer.seek(0)
            cursor.copy_expert(
                """
                COPY field_value_num (episode_id, field_id, value)
                FROM STDIN
                WITH (FORMAT text, DELIMITER E'\\t')
                """,
                buffer
            )
            
            self.conn.commit()
            logger.info(f"批量插入 {len(values_data)} 条数值型字段值")
            return len(values_data)
            
        except Exception as e:
            self.conn.rollback()
            logger.error(f"批量插入数值型字段值失败: {e}")
            raise
        finally:
            cursor.close()
    
    def batch_insert_field_values_text(self, values_data: List[Dict]) -> int:
        """
        批量插入文本型字段值（使用 COPY 优化）
        
        Args:
            values_data: 字段值数据列表，每项包含：
                - episode_id, field_id, value
                
        Returns:
            成功插入的数量
        """
        if not values_data:
            return 0
        
        cursor = self.conn.cursor()
        try:
            # 使用 COPY 批量插入
            buffer = StringIO()
            for item in values_data:
                # 转义文本中的特殊字符
                value = str(item['value']).replace('\\', '\\\\').replace('\n', '\\n').replace('\t', '\\t')
                row = f"{item['episode_id']}\t{item['field_id']}\t{value}\n"
                buffer.write(row)
            
            buffer.seek(0)
            cursor.copy_expert(
                """
                COPY field_value_text (episode_id, field_id, value)
                FROM STDIN
                WITH (FORMAT text, DELIMITER E'\\t')
                """,
                buffer
            )
            
            self.conn.commit()
            logger.info(f"批量插入 {len(values_data)} 条文本型字段值")
            return len(values_data)
            
        except Exception as e:
            self.conn.rollback()
            logger.error(f"批量插入文本型字段值失败: {e}")
            raise
        finally:
            cursor.close()
    
    def get_episodes_by_model(self, model: str, limit: int = None) -> List[str]:
        """
        获取指定 model 的所有 episode_id
        
        Args:
            model: 机器人型号
            limit: 限制数量
            
        Returns:
            episode_id 列表
        """
        cursor = self.conn.cursor()
        try:
            sql = """
                SELECT DISTINCT e.episode_id
                FROM episode e
                JOIN device d ON e.device_id = d.device_id
                WHERE d.model = %s
                ORDER BY e.episode_id
            """
            
            if limit:
                sql += f" LIMIT {limit}"
            
            cursor.execute(sql, (model,))
            return [row[0] for row in cursor.fetchall()]
        finally:
            cursor.close()
    
    def cleanup_updated_episode_field_values(self, model: str) -> Dict[str, int]:
        """
        只清理本次被更新的 episode 的字段值（不删除 field 定义）
        
        用于增量字段提取：只重新提取被更新的 episode 的字段值
        
        Args:
            model: 机器人型号
            
        Returns:
            清理统计信息
        """
        cursor = self.conn.cursor()
        stats = {
            'field_value_num_deleted': 0,
            'field_value_text_deleted': 0
        }
        
        try:
            # 1. 删除被标记为 updated 的 episode 的数值型字段值
            cursor.execute("""
                DELETE FROM field_value_num
                WHERE episode_id IN (
                    SELECT e.episode_id 
                    FROM episode e
                    JOIN device d ON e.device_id = d.device_id
                    WHERE d.model = %s AND e.is_updated = true
                )
            """, (model,))
            stats['field_value_num_deleted'] = cursor.rowcount
            
            # 2. 删除被标记为 updated 的 episode 的文本型字段值
            cursor.execute("""
                DELETE FROM field_value_text
                WHERE episode_id IN (
                    SELECT e.episode_id 
                    FROM episode e
                    JOIN device d ON e.device_id = d.device_id
                    WHERE d.model = %s AND e.is_updated = true
                )
            """, (model,))
            stats['field_value_text_deleted'] = cursor.rowcount
            
            self.conn.commit()
            
            logger.info(
                f"清理被更新 episode 的字段值 (model={model}): "
                f"numeric_values={stats['field_value_num_deleted']}, "
                f"text_values={stats['field_value_text_deleted']}"
            )
            
            return stats
            
        except Exception as e:
            self.conn.rollback()
            logger.error(f"清理被更新 episode 字段值失败 (model={model}): {e}")
            raise
        finally:
            cursor.close()
    
    def cleanup_field_data(self, model: str) -> Dict[str, int]:
        """
        清理指定 model 的所有字段数据
        
        删除顺序（因为外键约束）：
        1. field_value_num (依赖 field.field_id)
        2. field_value_text (依赖 field.field_id)
        3. field (主表)
        
        Args:
            model: 机器人型号
            
        Returns:
            清理统计信息
        """
        cursor = self.conn.cursor()
        stats = {
            'field_value_num_deleted': 0,
            'field_value_text_deleted': 0,
            'field_deleted': 0
        }
        
        try:
            # 1. 删除数值型字段值
            cursor.execute("""
                DELETE FROM field_value_num 
                WHERE field_id IN (
                    SELECT field_id FROM field WHERE model = %s
                )
            """, (model,))
            stats['field_value_num_deleted'] = cursor.rowcount
            
            # 2. 删除文本型字段值
            cursor.execute("""
                DELETE FROM field_value_text 
                WHERE field_id IN (
                    SELECT field_id FROM field WHERE model = %s
                )
            """, (model,))
            stats['field_value_text_deleted'] = cursor.rowcount
            
            # 3. 删除字段定义
            cursor.execute("""
                DELETE FROM field WHERE model = %s
            """, (model,))
            stats['field_deleted'] = cursor.rowcount
            
            self.conn.commit()
            
            logger.info(
                f"清理字段数据完成 (model={model}): "
                f"field={stats['field_deleted']}, "
                f"numeric_values={stats['field_value_num_deleted']}, "
                f"text_values={stats['field_value_text_deleted']}"
            )
            
            return stats
            
        except Exception as e:
            self.conn.rollback()
            logger.error(f"清理字段数据失败 (model={model}): {e}")
            raise
        finally:
            cursor.close()
    
    # ===== Rule 管理方法 =====
    
    def upsert_rule(
        self,
        presets_id: str,
        device_id: str,
        rule_type: str,
        rule_obs_bucket: str,
        rule_obs_path: str,
        rule_file_name: str,
        rule_file_time: datetime,
        csv_rule_txt: str = None
    ) -> int:
        """
        创建或更新规则（先删除旧的field_range）
        
        策略：彻底删除旧规则及其field_range，然后插入新规则
        
        Args:
            presets_id: preset ID
            device_id: 设备 ID
            rule_type: 规则类型 (base/full)
            rule_obs_bucket: OBS bucket
            rule_obs_path: OBS完整URI
            rule_file_name: 规则文件名
            rule_file_time: 规则文件时间
            csv_rule_txt: CSV完整内容
            
        Returns:
            rule_id
        """
        cursor = self.conn.cursor()
        try:
            # 1. 先删除旧规则（会级联删除field_range）
            cursor.execute("""
                DELETE FROM rule
                WHERE presets_id = %s AND device_id = %s AND rule_type = %s
            """, (presets_id, device_id, rule_type))
            
            if cursor.rowcount > 0:
                logger.info(f"删除旧规则: {presets_id}/{device_id}/{rule_type}")
            
            # 2. 插入新规则
            cursor.execute("""
                INSERT INTO rule (
                    presets_id, device_id, rule_type,
                    rule_obs_bucket, rule_obs_path,
                    rule_file_name, rule_file_time, csv_rule_txt
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING rule_id
            """, (
                presets_id, device_id, rule_type,
                rule_obs_bucket, rule_obs_path,
                rule_file_name, rule_file_time, csv_rule_txt
            ))
            
            rule_id = cursor.fetchone()[0]
            self.conn.commit()
            
            logger.info(f"创建新规则: rule_id={rule_id}, {presets_id}/{device_id}/{rule_type}")
            return rule_id
            
        except Exception as e:
            self.conn.rollback()
            logger.error(f"创建/更新规则失败: {e}")
            raise
        finally:
            cursor.close()
    
    def get_rule(
        self,
        presets_id: str,
        device_id: str,
        rule_type: str
    ) -> Optional[Dict]:
        """
        获取规则
        
        Args:
            presets_id: preset ID
            device_id: 设备 ID
            rule_type: 规则类型 (base/full)
            
        Returns:
            规则字典或None
        """
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                SELECT rule_id, presets_id, device_id, rule_type,
                       rule_obs_bucket, rule_obs_path,
                       rule_file_name, rule_file_time, csv_rule_txt,
                       created_at, updated_at
                FROM rule
                WHERE presets_id = %s AND device_id = %s AND rule_type = %s
            """, (presets_id, device_id, rule_type))
            
            row = cursor.fetchone()
            if row:
                return {
                    'rule_id': row[0],
                    'presets_id': row[1],
                    'device_id': str(row[2]),
                    'rule_type': row[3],
                    'rule_obs_bucket': row[4],
                    'rule_obs_path': row[5],
                    'rule_file_name': row[6],
                    'rule_file_time': row[7],
                    'csv_rule_txt': row[8],
                    'created_at': row[9],
                    'updated_at': row[10]
                }
            return None
        finally:
            cursor.close()
    
    def get_rule_by_id(self, rule_id: int) -> Optional[Dict]:
        """
        根据rule_id获取规则
        
        Args:
            rule_id: 规则ID
            
        Returns:
            规则字典或None
        """
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                SELECT rule_id, presets_id, device_id, rule_type,
                       rule_obs_bucket, rule_obs_path,
                       rule_file_name, rule_file_time, csv_rule_txt,
                       created_at, updated_at
                FROM rule
                WHERE rule_id = %s
            """, (rule_id,))
            
            row = cursor.fetchone()
            if row:
                return {
                    'rule_id': row[0],
                    'presets_id': row[1],
                    'device_id': str(row[2]),
                    'rule_type': row[3],
                    'rule_obs_bucket': row[4],
                    'rule_obs_path': row[5],
                    'rule_file_name': row[6],
                    'rule_file_time': row[7],
                    'csv_rule_txt': row[8],
                    'created_at': row[9],
                    'updated_at': row[10]
                }
            return None
        finally:
            cursor.close()
    
    # ===== Field Range 管理方法 =====
    
    def batch_get_field_ids(self, model: str, field_keys: List[str]) -> Dict[str, int]:
        """
        批量获取field_id
        
        Args:
            model: 机器人型号
            field_keys: field_key列表
            
        Returns:
            field_key -> field_id 的映射字典
        """
        if not field_keys:
            return {}
        
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                SELECT field_key, field_id
                FROM field
                WHERE model = %s AND field_key = ANY(%s)
            """, (model, field_keys))
            
            return {row[0]: row[1] for row in cursor.fetchall()}
        finally:
            cursor.close()
    
    def batch_insert_field_ranges(self, ranges_data: List[Dict]) -> Dict[str, int]:
        """
        批量插入字段范围（使用临时表 + COPY + ON CONFLICT 优化）
        
        策略：
        1. 创建临时表 (ON COMMIT DROP)
        2. COPY 数据到临时表 (高性能)
        3. INSERT ... SELECT ... ON CONFLICT (处理重复)
        
        Args:
            ranges_data: 字段范围数据列表，每项包含：
                - field_id, rule_id, min_range, max_range
                
        Returns:
            统计信息字典：
            {
                'total': 传入总数,
                'inserted': 实际插入数,
                'updated': 更新数 (如果使用DO UPDATE),
                'skipped': 跳过的重复数
            }
        """
        if not ranges_data:
            return {
                'total': 0,
                'inserted': 0,
                'updated': 0,
                'skipped': 0
            }
        
        cursor = self.conn.cursor()
        try:
            # 1. 创建临时表（ON COMMIT DROP）
            cursor.execute("""
                CREATE TEMP TABLE tmp_field_range (
                    field_id bigint,
                    rule_id bigint,
                    min_range numeric,
                    max_range numeric
                ) ON COMMIT DROP
            """)
            
            # 2. COPY 数据到临时表
            buffer = StringIO()
            for item in ranges_data:
                min_val = item.get('min_range')
                max_val = item.get('max_range')
                
                # 处理NULL值
                min_str = str(min_val) if min_val is not None else '\\N'
                max_str = str(max_val) if max_val is not None else '\\N'
                
                row = f"{item['field_id']}\t{item['rule_id']}\t{min_str}\t{max_str}\n"
                buffer.write(row)
            
            buffer.seek(0)
            cursor.copy_expert(
                """
                COPY tmp_field_range (field_id, rule_id, min_range, max_range)
                FROM STDIN
                WITH (FORMAT text, DELIMITER E'\\t', NULL '\\N')
                """,
                buffer
            )
            
            # 3. 从临时表插入到正式表，使用 ON CONFLICT
            # 注意：field_range 的主键是 (field_id, rule_id)
            # 由于我们在 upsert_rule 时已经删除了旧的 rule，
            # 这里通常不会有冲突，但为了鲁棒性还是用 ON CONFLICT
            cursor.execute("""
                WITH inserted AS (
                    INSERT INTO field_range (field_id, rule_id, min_range, max_range)
                    SELECT field_id, rule_id, min_range, max_range
                    FROM tmp_field_range
                    ON CONFLICT (field_id, rule_id) 
                    DO UPDATE SET
                        min_range = EXCLUDED.min_range,
                        max_range = EXCLUDED.max_range,
                        updated_at = now()
                    RETURNING field_id, 
                              CASE WHEN xmax = 0 THEN 'inserted' ELSE 'updated' END as action
                )
                SELECT 
                    COUNT(*) FILTER (WHERE action = 'inserted') as inserted_count,
                    COUNT(*) FILTER (WHERE action = 'updated') as updated_count
                FROM inserted
            """)
            
            result = cursor.fetchone()
            inserted_count = result[0] if result[0] else 0
            updated_count = result[1] if result[1] else 0
            
            # 4. 提交事务
            self.conn.commit()
            
            # 5. 统计信息
            total_count = len(ranges_data)
            skipped_count = total_count - inserted_count - updated_count
            
            stats = {
                'total': total_count,
                'inserted': inserted_count,
                'updated': updated_count,
                'skipped': skipped_count
            }
            
            logger.info(
                f"批量插入 field_range: 总数={total_count}, "
                f"插入={inserted_count}, 更新={updated_count}, 跳过={skipped_count}"
            )
            
            return stats
            
        except Exception as e:
            self.conn.rollback()
            logger.error(f"批量插入字段范围失败: {e}")
            raise
        finally:
            cursor.close()
    
    def get_field_ranges_by_rule(self, rule_id: int) -> List[Dict]:
        """
        获取指定规则的所有字段范围
        
        Args:
            rule_id: 规则ID
            
        Returns:
            字段范围列表
        """
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                SELECT fr.field_id, fr.rule_id, fr.min_range, fr.max_range,
                       f.field_key, f.field_name
                FROM field_range fr
                JOIN field f ON fr.field_id = f.field_id
                WHERE fr.rule_id = %s
                ORDER BY f.field_key
            """, (rule_id,))
            
            ranges = []
            for row in cursor.fetchall():
                ranges.append({
                    'field_id': row[0],
                    'rule_id': row[1],
                    'min_range': row[2],
                    'max_range': row[3],
                    'field_key': row[4],
                    'field_name': row[5]
                })
            
            return ranges
        finally:
            cursor.close()
