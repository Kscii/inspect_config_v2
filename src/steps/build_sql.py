# src/steps/build_sql.py
# -*- coding: utf-8 -*-
"""
build_sql step
职责：
1) 从 repo_root/csv_output/<model>/<model>_ranges.txt 读取规则配置 JSON
2) 生成 SQL 文件：repo_root/sql_output/update_rule_config.sql
注意：
- 你原项目里 SQL 的表结构/字段可能是固定的，这里提供“可直接用/易改”的模板式实现
- 若你有既定 SQL 模板（表名、字段名），后续我可以在第 3 次回复里把“模板字段”也做成配置项
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class BuildSqlResult:
    sql_path: Path


def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> BuildSqlResult:
    logger = runtime.get("logger")
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode", "normal"))

    models: List[str] = runtime["models"]
    model_to_ranges_txt: Dict[str, Path] = runtime["model_to_ranges_txt"]

    sql_dirname: str = str(step_cfg.get("sql_dirname", "sql_output"))
    sql_filename: str = str(step_cfg.get("sql_filename", "update_rule_config.sql"))
    csv_output_dirname: str = str(step_cfg.get("csv_output_dirname", "csv_output"))
    fail_if_missing: bool = bool(step_cfg.get("fail_if_missing", True))

    sql_dir = repo_root / sql_dirname
    sql_dir.mkdir(parents=True, exist_ok=True)
    out_sql = sql_dir / sql_filename

    lines: List[str] = []
    lines.append("")

    # 这里给一个“通用更新”模板：把 JSON 作为文本写入（你们数据库字段可能是 TEXT/JSON）
    # 你可以把 table/column 名换成你们实际的
    table_name = "rule_config"
    col_model = "model"
    col_payload = "payload_json"

    for model in models:
        txt_path = model_to_ranges_txt.get(model) or (repo_root / csv_output_dirname / model / f"{model}_ranges.txt")
        if not txt_path.exists():
            msg = f"[build_sql] model={model} 缺少 ranges.txt: {txt_path}"
            if fail_if_missing:
                raise FileNotFoundError(msg)
            _log(logger, log_mode, msg)
            continue

        payload = txt_path.read_text(encoding="utf-8")
        # SQL 里单引号转义
        payload_escaped = payload.replace("'", "''")

        lines.append(f"-- model: {model}")
        lines.append(
            f"UPDATE {table_name} SET {col_payload}='{payload_escaped}' WHERE {col_model}='{model}';"
        )
        lines.append("")

    out_sql.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _log(logger, log_mode, f"[build_sql] 输出 SQL -> {out_sql}")
    return BuildSqlResult(sql_path=out_sql)


def _log(logger, log_mode: str, msg: str) -> None:
    if logger is None:
        print(msg)
        return
    logger.info(msg)
