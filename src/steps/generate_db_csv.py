# src/steps/generate_db_csv.py
# -*- coding: utf-8 -*-
"""
generate_db_csv step
职责：
1) 生成用于导入 PostgreSQL 的 5 个 CSV 文件
2) 合并原 generate_info 的元数据提取功能
3) 输出到 db/<model>/ 目录下
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


@dataclass
class GenerateDbCsvResult:
    model_to_db_dir: Dict[str, Path]


def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> GenerateDbCsvResult:
    logger = runtime.get("logger")
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode", "normal"))

    obs_download_root: Path = runtime["obs_download_root"]
    models: List[str] = runtime["models"]

    # 读取配置
    json_suffix: str = str(step_cfg.get("json_suffix", "_collect.json"))
    csv_encoding: str = str(step_cfg.get("csv_encoding", "utf-8-sig"))
    db_dirname: str = str(step_cfg.get("db_dirname", "db"))
    default_value: str = str(step_cfg.get("default_value", "N/A"))
    
    metadata_selectors: Dict[str, str] = dict(step_cfg.get("metadata_selectors", {}))
    sn_selector = metadata_selectors.get(
        "sn",
        ".<report>.[<ruleCode>=<metadata_raw>].<rawDataMetric>.[<name>=<metadata.json>].<rawData>.<metadata>.<equipment_info>.<sn>"
    )
    collected_at_selector = metadata_selectors.get(
        "collected_at",
        ".<report>.[<ruleCode>=<metadata_raw>].<rawDataMetric>.[<name>=<metadata.json>].<rawData>.<metadata>.<collected_at>"
    )

    # 输入映射
    model_to_values_csv: Dict[str, Path] = runtime.get("model_to_values_csv", {}) or {}
    model_to_ranges_csv: Dict[str, Path] = runtime.get("model_to_ranges_csv", {}) or {}
    model_to_ranges_full_csv: Dict[str, Path] = runtime.get("model_to_ranges_full_csv", {}) or {}

    db_root = repo_root / db_dirname
    db_root.mkdir(parents=True, exist_ok=True)

    model_to_db_dir: Dict[str, Path] = {}

    for model in models:
        _log(logger, log_mode, f"[generate_db_csv] model={model} 开始处理")

        model_root = obs_download_root / model
        if not model_root.exists():
            _log(logger, log_mode, f"[generate_db_csv] model={model} 不存在目录：{model_root} -> 跳过")
            continue

        # 查找输入文件
        values_csv = model_to_values_csv.get(model)
        if not values_csv:
            values_csv = repo_root / "csv_output" / model / f"{model}.csv"
        if not values_csv or not values_csv.exists():
            _log(logger, log_mode, f"[generate_db_csv] model={model} 缺少 values.csv -> 跳过")
            continue

        ranges_csv = model_to_ranges_csv.get(model)
        if not ranges_csv:
            ranges_csv = repo_root / "csv_output" / model / f"{model}_ranges.csv"

        ranges_full_csv = model_to_ranges_full_csv.get(model)
        if not ranges_full_csv:
            ranges_full_csv = repo_root / "csv_output" / model / f"{model}_ranges_full.csv"

        # 扫描所有 JSON 文件
        json_files = [p for p in model_root.rglob(f"*{json_suffix}") if p.is_file()]
        if not json_files:
            _log(logger, log_mode, f"[generate_db_csv] model={model} 未找到 JSON 文件 -> 跳过")
            continue

        # 创建输出目录
        out_dir = db_root / model
        out_dir.mkdir(parents=True, exist_ok=True)

        # 读取 values.csv
        vdf = pd.read_csv(values_csv, encoding=csv_encoding)
        if vdf.empty:
            _log(logger, log_mode, f"[generate_db_csv] model={model} values.csv 为空 -> 跳过")
            continue

        # ========================================
        # 1. 生成 {model}_episode.csv
        # ========================================
        episode_rows = []
        episode_id_set = set()

        for json_path in json_files:
            try:
                data = json.loads(json_path.read_text(encoding="utf-8-sig", errors="ignore"))
            except Exception as e:
                _log(logger, log_mode, f"[generate_db_csv] 解析 JSON 失败：{json_path} err={e}")
                continue

            filename = json_path.name
            episode_id = _extract_episode_id(filename)
            
            # 去重
            if episode_id in episode_id_set:
                continue
            episode_id_set.add(episode_id)

            taskid = _extract_taskid_from_path(json_path, model_root, default_value)
            sn = _extract_value_by_selector(data, sn_selector, default_value)
            collected_at = _extract_value_by_selector(data, collected_at_selector, default_value)

            episode_rows.append({
                "episode_id": episode_id,
                "taskid": taskid,
                "model": model,
                "sn": sn,
                "collected_at": collected_at,
                "filename": filename,
            })

        episode_df = pd.DataFrame(episode_rows, columns=["episode_id", "taskid", "model", "sn", "collected_at", "filename"])
        episode_csv = out_dir / f"{model}_episode.csv"
        episode_df.to_csv(episode_csv, index=False, encoding=csv_encoding)
        _log(logger, log_mode, f"[generate_db_csv] model={model} 生成 episode.csv ({len(episode_rows)} 行)")

        # ========================================
        # 2. 生成 {model}_field.csv
        # ========================================
        # 获取所有字段（排除 episode_id）
        fields = [c for c in vdf.columns if c != "episode_id"]
        
        # 读取 ranges.csv 判断字段类型
        field_type_map = {}
        if ranges_csv and ranges_csv.exists():
            rdf = pd.read_csv(ranges_csv, encoding=csv_encoding)
            for _, row in rdf.iterrows():
                field = str(row["field"])
                min_val = str(row["min"])
                max_val = str(row["max"])
                # 判断类型：min/max 都是 "true" -> non_numeric
                if min_val.strip() == "true" and max_val.strip() == "true":
                    field_type_map[field] = "non_numeric"
                else:
                    field_type_map[field] = "numeric"

        field_rows = []
        for field_id, field in enumerate(fields, start=1):
            rule_code = _extract_rule_code(field)
            field_type = field_type_map.get(field, "")
            field_rows.append({
                "field_id": field_id,
                "field": field,
                "rule_code": rule_code,
                "type": field_type,
            })

        field_df = pd.DataFrame(field_rows, columns=["field_id", "field", "rule_code", "type"])
        field_csv = out_dir / f"{model}_field.csv"
        field_df.to_csv(field_csv, index=False, encoding=csv_encoding)
        _log(logger, log_mode, f"[generate_db_csv] model={model} 生成 field.csv ({len(field_rows)} 行)")

        # ========================================
        # 3. 生成 {model}_field_value.csv
        # ========================================
        # 构建 field -> field_id 映射
        field_to_id = {row["field"]: row["field_id"] for row in field_rows}

        # 转换为长格式
        field_value_rows = []
        for _, row in vdf.iterrows():
            episode_id = row["episode_id"]
            for field in fields:
                field_id = field_to_id.get(field)
                if field_id is None:
                    continue
                value = row[field]
                # 转换为字符串，处理 NaN
                if pd.isna(value):
                    value = ""
                else:
                    value = str(value)
                field_value_rows.append({
                    "episode_id": episode_id,
                    "field_id": field_id,
                    "value": value,
                })

        field_value_df = pd.DataFrame(field_value_rows, columns=["episode_id", "field_id", "value"])
        field_value_csv = out_dir / f"{model}_field_value.csv"
        field_value_df.to_csv(field_value_csv, index=False, encoding=csv_encoding)
        _log(logger, log_mode, f"[generate_db_csv] model={model} 生成 field_value.csv ({len(field_value_rows)} 行)")

        # ========================================
        # 4. 生成 {model}_thresholds_base.csv
        # ========================================
        if ranges_csv and ranges_csv.exists():
            rdf = pd.read_csv(ranges_csv, encoding=csv_encoding)
            # 确保列顺序
            required_cols = ["field", "min", "max", "pass_count", "fail_count", "pass_rate"]
            for col in required_cols:
                if col not in rdf.columns:
                    rdf[col] = ""
            thresholds_base_df = rdf[required_cols]
            thresholds_base_csv = out_dir / f"{model}_thresholds_base.csv"
            thresholds_base_df.to_csv(thresholds_base_csv, index=False, encoding=csv_encoding)
            _log(logger, log_mode, f"[generate_db_csv] model={model} 生成 thresholds_base.csv ({len(thresholds_base_df)} 行)")
        else:
            _log(logger, log_mode, f"[generate_db_csv] model={model} 缺少 ranges.csv，跳过 thresholds_base.csv")

        # ========================================
        # 5. 生成 {model}_thresholds_full.csv
        # ========================================
        if ranges_full_csv and ranges_full_csv.exists():
            rfdf = pd.read_csv(ranges_full_csv, encoding=csv_encoding)
            # 确保列顺序
            required_cols = ["field", "min", "max", "pass_count", "fail_count", "pass_rate"]
            for col in required_cols:
                if col not in rfdf.columns:
                    rfdf[col] = ""
            thresholds_full_df = rfdf[required_cols]
            thresholds_full_csv = out_dir / f"{model}_thresholds_full.csv"
            thresholds_full_df.to_csv(thresholds_full_csv, index=False, encoding=csv_encoding)
            _log(logger, log_mode, f"[generate_db_csv] model={model} 生成 thresholds_full.csv ({len(thresholds_full_df)} 行)")
        else:
            _log(logger, log_mode, f"[generate_db_csv] model={model} 缺少 ranges_full.csv，跳过 thresholds_full.csv")

        model_to_db_dir[model] = out_dir
        _log(logger, log_mode, f"[generate_db_csv] model={model} 完成 -> {out_dir}")

    return GenerateDbCsvResult(model_to_db_dir=model_to_db_dir)


# =========================
# 工具函数
# =========================

def _extract_episode_id(filename: str) -> str:
    """从文件名中提取 episode_id，文件名格式为 episodeid_collect.json"""
    if filename.endswith("_collect.json"):
        return filename[:-13]
    if filename.endswith(".json"):
        return filename[:-5]
    return filename


def _extract_taskid_from_path(json_path: Path, model_root: Path, default_value: str) -> str:
    """
    从路径中提取 taskid
    路径格式：obs_download/{model}/{taskid}/xxx/xxx.json
    taskid 是第一级子目录
    """
    try:
        rel_path = json_path.relative_to(model_root)
        parts = rel_path.parts
        if len(parts) > 0:
            return parts[0]
    except Exception:
        pass
    return default_value


def _extract_rule_code(field: str) -> str:
    """
    从 selector 字符串中提取 rule_code
    格式：[<ruleCode>=<xxx>]
    返回 xxx，如果没有则返回空字符串
    """
    match = re.search(r'\[<ruleCode>=<([^>]+)>\]', field)
    if match:
        return match.group(1)
    return ""


def _extract_value_by_selector(data: Any, selector: str, default_value: str) -> str:
    """
    通过 selector 从 JSON 中提取值
    selector 格式：.<key1>.[<filterKey>=<filterValue>].<key2>...
    """
    if not selector:
        return default_value

    try:
        return str(_navigate_selector(data, selector))
    except Exception:
        return default_value


def _navigate_selector(node: Any, selector: str) -> Any:
    """
    递归导航 selector 路径
    支持：
    - .<key>：普通字段访问
    - .[<key>=<value>]：列表过滤
    """
    if not selector or selector == "":
        return node

    # 去掉开头的 "."
    if selector.startswith("."):
        selector = selector[1:]

    if not selector:
        return node

    # 解析第一个 token
    # 两种模式：
    # 1. [<key>=<value>] - 列表过滤
    # 2. <key> - 字段访问

    if selector.startswith("["):
        # 列表过滤模式
        match = re.match(r"^\[<([^>]+)>=<([^>]+)>\](.*)$", selector)
        if not match:
            raise ValueError(f"无效的列表过滤 selector: {selector}")

        filter_key = match.group(1)
        filter_value = match.group(2)
        rest = match.group(3)

        if not isinstance(node, list):
            raise ValueError(f"节点不是列表，无法应用过滤器: {selector}")

        # 查找匹配的元素
        for item in node:
            if isinstance(item, dict) and str(item.get(filter_key)) == filter_value:
                return _navigate_selector(item, rest)

        raise ValueError(f"列表中未找到匹配项: {filter_key}={filter_value}")

    else:
        # 字段访问模式
        # 提取第一个 token（到下一个 "." 或 "[" 为止）
        match = re.match(r"^<([^>]+)>(.*)$", selector)
        if match:
            # 使用 <key> 格式
            key = match.group(1)
            rest = match.group(2)
        else:
            # 简单字段名（没有 <>）
            next_sep = min(
                (selector.find(".") if selector.find(".") >= 0 else len(selector)),
                (selector.find("[") if selector.find("[") >= 0 else len(selector))
            )
            key = selector[:next_sep]
            rest = selector[next_sep:]

        if not isinstance(node, dict):
            raise ValueError(f"节点不是字典，无法访问字段: {key}")

        if key not in node:
            raise ValueError(f"字段不存在: {key}")

        return _navigate_selector(node[key], rest)


def _log(logger, log_mode: str, msg: str) -> None:
    if logger is None:
        print(msg)
        return
    logger.info(msg)
