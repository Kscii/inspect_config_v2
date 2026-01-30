# src/steps/generate_info.py
# -*- coding: utf-8 -*-
"""
generate_info step
职责：
1) 扫描 obs_download/<model>/... 中的 JSON 文件（默认 *_collect.json）
2) 提取固定字段：file, episodeId, taskid, model, SN, createdAt
3) 支持配置额外列（通过 selector 从 JSON 提取，可按构型过滤）
4) 输出：csv_output/<model>/<model>_info.csv
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd


@dataclass
class GenerateInfoResult:
    model_to_info_csv: Dict[str, Path]


def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> GenerateInfoResult:
    logger = runtime.get("logger")
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode", "normal"))
    obs_download_root: Path = runtime["obs_download_root"]
    models: List[str] = runtime["models"]

    csv_output_dir = repo_root / "csv_output"
    csv_output_dir.mkdir(parents=True, exist_ok=True)

    # 读取配置
    json_suffix: str = str(step_cfg.get("json_suffix", "_collect.json"))
    csv_encoding: str = str(step_cfg.get("csv_encoding", "utf-8-sig"))
    default_value: str = str(step_cfg.get("default_value", "N/A"))
    extra_columns: List[Dict[str, Any]] = list(step_cfg.get("extra_columns", []))

    model_to_csv: Dict[str, Path] = {}

    for model in models:
        model_root = obs_download_root / model
        if not model_root.exists():
            _log(logger, log_mode, f"[generate_info] model={model} 不存在目录：{model_root} -> 跳过")
            continue

        # 扫描所有 JSON 文件
        json_files = [p for p in model_root.rglob(f"*{json_suffix}") if p.is_file()]
        if not json_files:
            _log(logger, log_mode, f"[generate_info] model={model} 未找到 JSON 文件 -> 跳过")
            continue

        _log(logger, log_mode, f"[generate_info] model={model} 找到 {len(json_files)} 个 JSON 文件")

        # 筛选适用于当前构型的额外列
        applicable_extra_cols = _filter_extra_columns(extra_columns, model)

        # 提取数据
        rows = []
        for json_path in json_files:
            row = _extract_row(
                json_path=json_path,
                model=model,
                model_root=model_root,
                default_value=default_value,
                extra_columns=applicable_extra_cols,
                logger=logger,
                log_mode=log_mode,
            )
            if row:
                rows.append(row)

        if not rows:
            _log(logger, log_mode, f"[generate_info] model={model} 提取失败，无有效数据 -> 跳过")
            continue

        # 构建 DataFrame
        df = pd.DataFrame(rows)

        # 确保列顺序：固定列在前，额外列在后
        fixed_cols = ["file", "episodeId", "taskid", "model", "SN", "createdAt"]
        extra_col_names = [col["column_name"] for col in applicable_extra_cols]
        all_cols = fixed_cols + extra_col_names

        # 重新排序列（只包含存在的列）
        existing_cols = [c for c in all_cols if c in df.columns]
        df = df[existing_cols]

        # 输出 CSV
        out_dir = csv_output_dir / model
        out_dir.mkdir(parents=True, exist_ok=True)
        out_csv = out_dir / f"{model}_info.csv"
        df.to_csv(out_csv, index=False, encoding=csv_encoding)
        model_to_csv[model] = out_csv

        _log(logger, log_mode, f"[generate_info] model={model} 输出 {len(rows)} 行数据 -> {out_csv}")

    return GenerateInfoResult(model_to_info_csv=model_to_csv)


def _filter_extra_columns(extra_columns: List[Dict[str, Any]], model: str) -> List[Dict[str, Any]]:
    """筛选适用于当前构型的额外列"""
    result = []
    for col in extra_columns:
        models_filter = col.get("models", [])
        if not models_filter:
            # 未指定 models，应用到所有构型
            result.append(col)
        elif model in models_filter:
            # 当前构型在列表中
            result.append(col)
    return result


def _extract_row(
    json_path: Path,
    model: str,
    model_root: Path,
    default_value: str,
    extra_columns: List[Dict[str, Any]],
    logger,
    log_mode: str,
) -> Optional[Dict[str, str]]:
    """从单个 JSON 文件提取一行数据"""
    try:
        data = json.loads(json_path.read_text(encoding="utf-8-sig", errors="ignore"))
    except Exception as e:
        _log(logger, log_mode, f"[generate_info] 解析 JSON 失败：{json_path} err={e}")
        return None

    row = {}

    # 固定列 1: file (basename)
    row["file"] = json_path.name

    # 固定列 2: episodeId
    row["episodeId"] = str(data.get("episodeId", default_value))

    # 固定列 3: taskid (从路径提取)
    row["taskid"] = _extract_taskid_from_path(json_path, model_root, default_value)

    # 固定列 4: model
    row["model"] = model

    # 固定列 5: SN (从 JSON 深层路径提取)
    sn_selector = ".<report>.[<ruleCode>=<metadata_raw>].<rawDataMetric>.[<name>=<metadata.json>].<rawData>.<metadata>.<equipment_info>.<sn>"
    row["SN"] = _extract_value_by_selector(data, sn_selector, default_value)

    # 固定列 6: createdAt
    row["createdAt"] = str(data.get("createdAt", default_value))

    # 额外列
    for col in extra_columns:
        col_name = col.get("column_name", "unknown")
        selector = col.get("selector", "")
        row[col_name] = _extract_value_by_selector(data, selector, default_value)

    return row


def _extract_taskid_from_path(json_path: Path, model_root: Path, default_value: str) -> str:
    """
    从路径中提取 taskid
    路径格式：obs_download/{model}/{taskid}/s{xxx}/xxx.json
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
