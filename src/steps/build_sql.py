# src/steps/build_sql.py
# -*- coding: utf-8 -*-
"""
build_sql step
职责：
1) 从 ranges.txt 读取规则配置（literal 单字符串）
2) 生成 SQL 文件
3) 支持 base 与 full 各生成一份 SQL
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class BuildSqlResult:
    sql_path: Path
    sql_path_full: Optional[Path]


def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> BuildSqlResult:
    logger = runtime.get("logger")
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode", "normal"))

    enable_full: bool = bool(step_cfg.get("enable_full", True))

    models: List[str] = runtime["models"]
    model_to_ranges_txt: Dict[str, Path] = runtime.get("model_to_ranges_txt", {}) or {}
    model_to_ranges_full_txt: Dict[str, Path] = runtime.get("model_to_ranges_full_txt", {}) or {}

    sql_dirname: str = str(step_cfg.get("sql_dirname", "sql_output"))
    sql_filename: str = str(step_cfg.get("sql_filename", "update_rule_config.sql"))
    sql_filename_full: str = str(step_cfg.get("sql_filename_full", "update_rule_config_full.sql"))

    csv_output_dirname: str = str(step_cfg.get("csv_output_dirname", "csv_output"))
    fail_if_missing: bool = bool(step_cfg.get("fail_if_missing", True))

    sql_dir = repo_root / sql_dirname
    sql_dir.mkdir(parents=True, exist_ok=True)

    # base
    out_sql = sql_dir / sql_filename
    _build_one_sql(
        variant="base",
        repo_root=repo_root,
        logger=logger,
        log_mode=log_mode,
        models=models,
        model_to_ranges_txt=model_to_ranges_txt,
        fixed_txt_name_tpl="{model}_ranges.txt",
        csv_output_dirname=csv_output_dirname,
        out_sql=out_sql,
        fail_if_missing=fail_if_missing,
    )

    out_sql_full: Optional[Path] = None
    if enable_full:
        out_sql_full = sql_dir / sql_filename_full
        _build_one_sql(
            variant="full",
            repo_root=repo_root,
            logger=logger,
            log_mode=log_mode,
            models=models,
            model_to_ranges_txt=model_to_ranges_full_txt,
            fixed_txt_name_tpl="{model}_ranges_full.txt",
            csv_output_dirname=csv_output_dirname,
            out_sql=out_sql_full,
            fail_if_missing=fail_if_missing,
        )

    return BuildSqlResult(sql_path=out_sql, sql_path_full=out_sql_full)


def _build_one_sql(
    variant: str,
    repo_root: Path,
    logger,
    log_mode: str,
    models: List[str],
    model_to_ranges_txt: Dict[str, Path],
    fixed_txt_name_tpl: str,
    csv_output_dirname: str,
    out_sql: Path,
    fail_if_missing: bool,
) -> None:
    lines: List[str] = []
    lines.append("")

    table_name = "rule_config"
    col_model = "model"
    col_payload = "payload_json"

    for model in models:
        fixed_txt = repo_root / csv_output_dirname / model / fixed_txt_name_tpl.format(model=model)
        fallback = model_to_ranges_txt.get(model)
        txt_path = fixed_txt if fixed_txt.exists() else (Path(fallback) if fallback else None)

        if not txt_path or not txt_path.exists():
            msg = f"[build_sql:{variant}] model={model} 缺少 ranges.txt: {fixed_txt}"
            if fail_if_missing:
                raise FileNotFoundError(msg)
            _log(logger, log_mode, msg)
            continue

        payload = txt_path.read_text(encoding="utf-8")
        payload_escaped = payload.replace("'", "''")

        lines.append(f"-- variant: {variant} | model: {model}")
        lines.append(
            f"UPDATE {table_name} SET {col_payload}='{payload_escaped}' WHERE {col_model}='{model}';"
        )
        lines.append("")

    out_sql.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _log(logger, log_mode, f"[build_sql:{variant}] 输出 SQL -> {out_sql}")


def _log(logger, log_mode: str, msg: str) -> None:
    if logger is None:
        print(msg)
        return
    logger.info(msg)
