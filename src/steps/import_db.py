# src/steps/import_db.py
# -*- coding: utf-8 -*-
"""
import_db step
职责：
1) 将 generate_db_csv 生成的 CSV 文件导入到本地 PostgreSQL 数据库
2) 每个 model 使用独立 schema（如 a2, gr2）
3) 动态创建表结构（如果不存在）
4) 每次导入前 TRUNCATE 清空旧数据
5) 使用 COPY 命令高效导入
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
except ImportError:
    psycopg2 = None


@dataclass
class ImportDbResult:
    imported_models: List[str]


def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> ImportDbResult:
    if psycopg2 is None:
        raise ImportError(
            "psycopg2 未安装。请运行：uv add psycopg2-binary\n"
            "或在 pyproject.toml 的 dependencies 中添加 psycopg2-binary"
        )

    logger = runtime.get("logger")
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode", "normal"))

    models: List[str] = runtime["models"]
    model_to_db_dir: Dict[str, Path] = runtime.get("model_to_db_dir", {}) or {}

    # 读取配置
    host: str = str(step_cfg.get("host", "localhost"))
    port: int = int(step_cfg.get("port", 5432))
    database: str = str(step_cfg.get("database", "inspectdb"))
    user: str = str(step_cfg.get("user", "inspect"))
    password: str = str(step_cfg.get("password", "inspect_pw"))

    truncate_before_import: bool = bool(step_cfg.get("truncate_before_import", True))
    schema_name_template: str = str(step_cfg.get("schema_name_template", "{model_lower}"))
    create_tables_if_missing: bool = bool(step_cfg.get("create_tables_if_missing", True))
    enable_full: bool = bool(step_cfg.get("enable_full", True))
    dry_run: bool = bool(step_cfg.get("dry_run", False)) or bool(global_cfg.get("dry_run", False))

    db_dirname: str = str(step_cfg.get("db_dirname", "db"))
    db_root = repo_root / db_dirname

    imported_models: List[str] = []

    _log(logger, log_mode, f"[import_db] 连接数据库：{user}@{host}:{port}/{database}")

    # 建立数据库连接
    try:
        conn = psycopg2.connect(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
        )
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    except Exception as e:
        raise RuntimeError(f"[import_db] 数据库连接失败：{e}")

    try:
        for model in models:
            _log(logger, log_mode, f"[import_db] model={model} 开始导入")

            # 查找 CSV 目录
            db_dir = model_to_db_dir.get(model)
            if not db_dir:
                db_dir = db_root / model
            if not db_dir or not db_dir.exists():
                _log(logger, log_mode, f"[import_db] model={model} 缺少 db 目录 -> 跳过")
                continue

            # 生成 schema 名称（转小写）
            schema = schema_name_template.format(model_lower=model.lower(), model=model)

            try:
                _import_model(
                    conn=conn,
                    model=model,
                    schema=schema,
                    db_dir=db_dir,
                    truncate_before_import=truncate_before_import,
                    create_tables_if_missing=create_tables_if_missing,
                    enable_full=enable_full,
                    dry_run=dry_run,
                    logger=logger,
                    log_mode=log_mode,
                )
                imported_models.append(model)
                _log(logger, log_mode, f"[import_db] model={model} 导入完成")
            except Exception as e:
                conn.rollback()
                raise RuntimeError(f"[import_db] model={model} 导入失败：{e}") from e

    finally:
        conn.close()

    _log(logger, log_mode, f"[import_db] 全部完成，共导入 {len(imported_models)} 个 model")
    return ImportDbResult(imported_models=imported_models)


def _import_model(
    conn,
    model: str,
    schema: str,
    db_dir: Path,
    truncate_before_import: bool,
    create_tables_if_missing: bool,
    enable_full: bool,
    dry_run: bool,
    logger,
    log_mode: str,
) -> None:
    """导入单个 model 的所有表"""
    cur = conn.cursor()

    try:
        # 1. 创建 schema
        if create_tables_if_missing:
            if dry_run:
                _log(logger, log_mode, f"[import_db][DRY-RUN] 创建 schema: {schema}")
            else:
                cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
                _log(logger, log_mode, f"[import_db] 创建 schema: {schema}")

        # 2. 创建表结构
        if create_tables_if_missing:
            _create_tables(cur, schema, dry_run, logger, log_mode)

        # 3. 导入数据（按顺序：episode → field → thresholds → field_value）
        # episode
        episode_csv = db_dir / f"{model}_episode.csv"
        if episode_csv.exists():
            _import_table(
                cur=cur,
                schema=schema,
                table="episode",
                csv_path=episode_csv,
                truncate=truncate_before_import,
                dry_run=dry_run,
                logger=logger,
                log_mode=log_mode,
            )

        # field
        field_csv = db_dir / f"{model}_field.csv"
        if field_csv.exists():
            _import_table(
                cur=cur,
                schema=schema,
                table="field",
                csv_path=field_csv,
                truncate=truncate_before_import,
                dry_run=dry_run,
                logger=logger,
                log_mode=log_mode,
            )

        # thresholds_base
        thresholds_base_csv = db_dir / f"{model}_thresholds_base.csv"
        if thresholds_base_csv.exists():
            _import_table(
                cur=cur,
                schema=schema,
                table="thresholds_base",
                csv_path=thresholds_base_csv,
                truncate=truncate_before_import,
                dry_run=dry_run,
                logger=logger,
                log_mode=log_mode,
            )

        # thresholds_full
        if enable_full:
            thresholds_full_csv = db_dir / f"{model}_thresholds_full.csv"
            if thresholds_full_csv.exists():
                _import_table(
                    cur=cur,
                    schema=schema,
                    table="thresholds_full",
                    csv_path=thresholds_full_csv,
                    truncate=truncate_before_import,
                    dry_run=dry_run,
                    logger=logger,
                    log_mode=log_mode,
                )

        # field_value (最后导入，因为有外键依赖)
        field_value_csv = db_dir / f"{model}_field_value.csv"
        if field_value_csv.exists():
            _import_table(
                cur=cur,
                schema=schema,
                table="field_value",
                csv_path=field_value_csv,
                truncate=truncate_before_import,
                dry_run=dry_run,
                logger=logger,
                log_mode=log_mode,
            )

    finally:
        cur.close()


def _create_tables(cur, schema: str, dry_run: bool, logger, log_mode: str) -> None:
    """创建表结构（如果不存在）"""
    
    # 表定义（按照 CSV 列名）
    tables = {
        "episode": """
            CREATE TABLE IF NOT EXISTS {schema}.episode (
                episode_id TEXT PRIMARY KEY,
                taskid TEXT,
                model TEXT,
                sn TEXT,
                filename TEXT
            )
        """,
        "field": """
            CREATE TABLE IF NOT EXISTS {schema}.field (
                field_id BIGINT PRIMARY KEY,
                field TEXT UNIQUE NOT NULL,
                rule_code TEXT,
                type TEXT
            )
        """,
        "thresholds_base": """
            CREATE TABLE IF NOT EXISTS {schema}.thresholds_base (
                field TEXT PRIMARY KEY,
                min TEXT,
                max TEXT,
                pass_count BIGINT,
                fail_count BIGINT,
                pass_rate DOUBLE PRECISION
            )
        """,
        "thresholds_full": """
            CREATE TABLE IF NOT EXISTS {schema}.thresholds_full (
                field TEXT PRIMARY KEY,
                min TEXT,
                max TEXT,
                pass_count BIGINT,
                fail_count BIGINT,
                pass_rate DOUBLE PRECISION
            )
        """,
        "field_value": """
            CREATE TABLE IF NOT EXISTS {schema}.field_value (
                episode_id TEXT NOT NULL,
                field_id BIGINT NOT NULL,
                value TEXT,
                PRIMARY KEY (episode_id, field_id)
            )
        """,
    }

    for table_name, create_sql in tables.items():
        if dry_run:
            _log(logger, log_mode, f"[import_db][DRY-RUN] 创建表: {schema}.{table_name}")
        else:
            cur.execute(create_sql.format(schema=sql.Identifier(schema).as_string(cur)))
            _log(logger, log_mode, f"[import_db] 创建表: {schema}.{table_name}")

    # 创建索引
    if not dry_run:
        indexes = [
            f"CREATE INDEX IF NOT EXISTS idx_{schema}_field_value_field_id ON {schema}.field_value(field_id)",
            f"CREATE INDEX IF NOT EXISTS idx_{schema}_episode_sn ON {schema}.episode(sn)",
        ]
        for idx_sql in indexes:
            try:
                cur.execute(idx_sql)
            except Exception:
                pass  # 索引可能已存在


def _import_table(
    cur,
    schema: str,
    table: str,
    csv_path: Path,
    truncate: bool,
    dry_run: bool,
    logger,
    log_mode: str,
) -> None:
    """使用 COPY 命令导入单个表"""
    
    if not csv_path.exists():
        _log(logger, log_mode, f"[import_db] CSV 不存在：{csv_path} -> 跳过")
        return

    # TRUNCATE 清空表
    if truncate:
        if dry_run:
            _log(logger, log_mode, f"[import_db][DRY-RUN] TRUNCATE {schema}.{table}")
        else:
            truncate_sql = sql.SQL("TRUNCATE TABLE {}.{} CASCADE").format(
                sql.Identifier(schema),
                sql.Identifier(table),
            )
            cur.execute(truncate_sql)
            _log(logger, log_mode, f"[import_db] TRUNCATE {schema}.{table}")

    # COPY 导入
    if dry_run:
        _log(logger, log_mode, f"[import_db][DRY-RUN] COPY {csv_path} -> {schema}.{table}")
    else:
        copy_sql = sql.SQL("COPY {}.{} FROM STDIN WITH (FORMAT CSV, HEADER TRUE, ENCODING 'UTF8')").format(
            sql.Identifier(schema),
            sql.Identifier(table),
        )
        
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            try:
                cur.copy_expert(copy_sql.as_string(cur), f)
                _log(logger, log_mode, f"[import_db] 导入成功：{csv_path} -> {schema}.{table}")
            except Exception as e:
                raise RuntimeError(f"[import_db] 导入失败：{csv_path} -> {schema}.{table}，错误：{e}") from e


def _log(logger, log_mode: str, msg: str) -> None:
    if logger is None:
        print(msg)
        return
    logger.info(msg)
