# src/steps/test_range.py
# -*- coding: utf-8 -*-
"""
test_range step
职责：
1) 读取 values.csv 与 ranges.csv
2) 对每个字段统计通过率（pass_count/fail_count/pass_rate）
3) 将统计写回 ranges.csv（覆盖写回或输出到新路径）
4) 控制打印 TopN / 失败明细等
5) 支持对 base 与 full 两套 ranges 各跑一次
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import numpy as np


@dataclass
class TestRangeResult:
    model_to_ranges_csv: Dict[str, Path]
    model_to_ranges_full_csv: Dict[str, Path]


def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> TestRangeResult:
    logger = runtime.get("logger")
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode", "normal"))

    enable_full: bool = bool(step_cfg.get("enable_full", True))

    models: List[str] = runtime["models"]
    
    # values_csv：优先从 runtime 获取，否则使用默认路径
    model_to_values_csv: Dict[str, Path] = runtime.get("model_to_values_csv", {}) or {}
    if not model_to_values_csv:
        # 自动从默认路径构建 model_to_values_csv
        csv_output_dirname = repo_root / "csv_output"
        for model in models:
            default_values = csv_output_dirname / model / f"{model}.csv"
            if default_values.exists():
                model_to_values_csv[model] = default_values

    # base/full 输入映射（full 若缺失则空 dict）
    model_to_ranges_csv_in: Dict[str, Path] = runtime.get("model_to_ranges_csv", {}) or {}
    model_to_ranges_full_csv_in: Dict[str, Path] = runtime.get("model_to_ranges_full_csv", {}) or {}

    output_ranges_csv = step_cfg.get("output_ranges_csv", None)

    treat_nan_as_fail: bool = bool(step_cfg.get("treat_nan_as_fail", True))
    fields_mode: str = str(step_cfg.get("fields_mode", "range_only"))

    missing_value_col_is_fail: bool = bool(step_cfg.get("missing_value_col_is_fail", True))
    missing_range_is_fail: bool = bool(step_cfg.get("missing_range_is_fail", False))

    show_top_fields: bool = bool(step_cfg.get("show_top_fields", True))
    top_n_fields: int = int(step_cfg.get("top_n_fields", 5))

    show_fail_details: bool = bool(step_cfg.get("show_fail_details", True))
    max_fail_details = step_cfg.get("max_fail_details", 1)
    if max_fail_details is None:
        max_fail_details = 0
    max_fail_details = int(max_fail_details)

    # Episode 通过率配置
    enable_episode_passrate: bool = bool(step_cfg.get("enable_episode_passrate", False))
    episode_passrate_filename: str = str(step_cfg.get("episode_passrate_filename", "{model}_episode_passrate.csv"))
    show_top_failed_episodes: bool = bool(step_cfg.get("show_top_failed_episodes", True))
    top_n_failed_episodes: int = int(step_cfg.get("top_n_failed_episodes", 10))

    out_base: Dict[str, Path] = {}
    out_full: Dict[str, Path] = {}

    # 跑 base
    out_base = _run_one_variant(
        variant="base",
        repo_root=repo_root,
        logger=logger,
        log_mode=log_mode,
        models=models,
        model_to_values_csv=model_to_values_csv,
        model_to_ranges_csv_in=model_to_ranges_csv_in,
        fixed_ranges_name_tpl="{model}_ranges.csv",
        output_ranges_csv=output_ranges_csv,
        treat_nan_as_fail=treat_nan_as_fail,
        fields_mode=fields_mode,
        missing_value_col_is_fail=missing_value_col_is_fail,
        missing_range_is_fail=missing_range_is_fail,
        show_top_fields=show_top_fields,
        top_n_fields=top_n_fields,
        show_fail_details=show_fail_details,
        max_fail_details=max_fail_details,
        enable_episode_passrate=enable_episode_passrate,
        episode_passrate_filename=episode_passrate_filename,
        show_top_failed_episodes=show_top_failed_episodes,
        top_n_failed_episodes=top_n_failed_episodes,
    )

    # 跑 full
    if enable_full:
        out_full = _run_one_variant(
            variant="full",
            repo_root=repo_root,
            logger=logger,
            log_mode=log_mode,
            models=models,
            model_to_values_csv=model_to_values_csv,
            model_to_ranges_csv_in=model_to_ranges_full_csv_in,
            fixed_ranges_name_tpl="{model}_ranges_full.csv",
            output_ranges_csv=output_ranges_csv,
            treat_nan_as_fail=treat_nan_as_fail,
            fields_mode=fields_mode,
            missing_value_col_is_fail=missing_value_col_is_fail,
            missing_range_is_fail=missing_range_is_fail,
            show_top_fields=show_top_fields,
            top_n_fields=top_n_fields,
            show_fail_details=show_fail_details,
            max_fail_details=max_fail_details,
            enable_episode_passrate=enable_episode_passrate,
            episode_passrate_filename=episode_passrate_filename,
            show_top_failed_episodes=show_top_failed_episodes,
            top_n_failed_episodes=top_n_failed_episodes,
        )

    return TestRangeResult(model_to_ranges_csv=out_base, model_to_ranges_full_csv=out_full)


def _run_one_variant(
    variant: str,
    repo_root: Path,
    logger,
    log_mode: str,
    models: List[str],
    model_to_values_csv: Dict[str, Path],
    model_to_ranges_csv_in: Dict[str, Path],
    fixed_ranges_name_tpl: str,
    output_ranges_csv: Any,
    treat_nan_as_fail: bool,
    fields_mode: str,
    missing_value_col_is_fail: bool,
    missing_range_is_fail: bool,
    show_top_fields: bool,
    top_n_fields: int,
    show_fail_details: bool,
    max_fail_details: int,
    enable_episode_passrate: bool,
    episode_passrate_filename: str,
    show_top_failed_episodes: bool,
    top_n_failed_episodes: int,
) -> Dict[str, Path]:
    out_map: Dict[str, Path] = {}

    for model in models:
        values_csv = model_to_values_csv.get(model)

        # ranges 输入：优先固定路径，其次 runtime 映射
        fixed_csv = repo_root / "csv_output" / model / fixed_ranges_name_tpl.format(model=model)
        fallback_csv = model_to_ranges_csv_in.get(model)
        ranges_csv = fixed_csv if fixed_csv.exists() else (Path(fallback_csv) if fallback_csv else None)

        if not values_csv or not Path(values_csv).exists():
            _log(logger, log_mode, f"[test_range:{variant}] model={model} 缺少 values.csv -> 跳过")
            continue
        if not ranges_csv or not Path(ranges_csv).exists():
            _log(logger, log_mode, f"[test_range:{variant}] model={model} 缺少 ranges.csv -> 跳过")
            continue

        vdf = pd.read_csv(values_csv, encoding="utf-8-sig")
        rdf = pd.read_csv(ranges_csv, encoding="utf-8-sig")
        if vdf.empty or rdf.empty:
            _log(logger, log_mode, f"[test_range:{variant}] model={model} 空表 -> 跳过")
            continue

        # 统一列名：要求 find_range 输出 field/min/max/non_numeric
        if "field" not in rdf.columns or "min" not in rdf.columns or "max" not in rdf.columns:
            raise ValueError(f"[test_range:{variant}] ranges.csv 列不符合预期：{ranges_csv} columns={list(rdf.columns)}")

        range_fields = rdf["field"].astype(str).tolist()
        value_fields = [c for c in vdf.columns if c != "file"]

        if fields_mode == "values_only":
            fields = value_fields
        else:
            fields = range_fields

        # 预构建 ranges dict
        ranges_map: Dict[str, Tuple[Any, Any, bool]] = {}
        for _, row in rdf.iterrows():
            f = str(row["field"])
            mn = row["min"]
            mx = row["max"]
            non_numeric = str(row.get("non_numeric", "")).strip() != ""
            ranges_map[f] = (mn, mx, non_numeric)

        # 统计
        field_stats = []
        fail_details = []

        for f in fields:
            in_range = f in ranges_map
            in_values = f in vdf.columns

            if not in_range:
                if fields_mode == "values_only":
                    # values_only 才可能出现这种情况
                    if missing_range_is_fail:
                        # 没范围 -> 全 fail
                        cnt = len(vdf)
                        field_stats.append((f, 0, cnt, 0.0))
                    else:
                        continue
                else:
                    continue

            mn, mx, non_numeric = ranges_map.get(f, ("", "", True))

            if not in_values:
                if missing_value_col_is_fail:
                    cnt = len(vdf)
                    field_stats.append((f, 0, cnt, 0.0))
                    if show_fail_details:
                        fail_details.append((model, "<ALL_FILES>", f, "missing_value_column"))
                else:
                    continue
                continue

            # 非数值字段：用 true/true 或 non_numeric 列判断
            if non_numeric or (str(mn) == "true" and str(mx) == "true"):
                # 非数值字段：向量化验证“非空”
                col_data = vdf[f]
                # 检查非空、非NaN、非空字符串
                valid_mask = pd.notna(col_data) & (col_data.astype(str).str.strip() != "") & (col_data.astype(str).str.lower() != "nan")
                
                pass_cnt = int(valid_mask.sum())
                fail_cnt = int((~valid_mask).sum())
                rate = pass_cnt / (pass_cnt + fail_cnt) if (pass_cnt + fail_cnt) else 0.0
                field_stats.append((f, pass_cnt, fail_cnt, rate))
                
                # 收集失败详情（限制数量）
                if show_fail_details and fail_cnt > 0:
                    fail_indices = vdf.index[~valid_mask].tolist()
                    limit = min(len(fail_indices), max_fail_details) if max_fail_details > 0 else len(fail_indices)
                    for idx in fail_indices[:limit]:
                        file_name = str(vdf.loc[idx, "file"]) if "file" in vdf.columns else ""
                        fail_details.append((model, file_name, f, "empty"))
                continue

            # 数值字段：区间检测
            try:
                mn_f = float(str(mn).strip())
                mx_f = float(str(mx).strip())
            except Exception:
                # 解析不了就当非数值（更保守）
                pass_cnt = 0
                fail_cnt = len(vdf)
                field_stats.append((f, pass_cnt, fail_cnt, 0.0))
                continue

            # 向量化数值检测
            col_data = vdf[f]
            
            # 转换为数值，无法转换的变为 NaN
            numeric_data = pd.to_numeric(col_data, errors='coerce')
            
            # NaN 处理
            if treat_nan_as_fail:
                # NaN 算失败
                valid_mask = pd.notna(numeric_data) & (numeric_data >= mn_f) & (numeric_data <= mx_f)
                pass_cnt = int(valid_mask.sum())
                fail_cnt = len(vdf) - pass_cnt
            else:
                # NaN 不计入统计
                not_nan_mask = pd.notna(numeric_data)
                in_range_mask = (numeric_data >= mn_f) & (numeric_data <= mx_f)
                valid_mask = not_nan_mask & in_range_mask
                
                pass_cnt = int(valid_mask.sum())
                fail_cnt = int(not_nan_mask.sum() - pass_cnt)
            
            total = pass_cnt + fail_cnt
            rate = pass_cnt / total if total else 0.0
            field_stats.append((f, pass_cnt, fail_cnt, rate))
            
            # 收集失败详情（限制数量）
            if show_fail_details and fail_cnt > 0:
                if treat_nan_as_fail:
                    # NaN 和超出范围都是失败
                    fail_mask = ~valid_mask
                else:
                    # 只有超出范围算失败（排除 NaN）
                    fail_mask = not_nan_mask & ~in_range_mask
                
                fail_indices = vdf.index[fail_mask].tolist()
                limit = min(len(fail_indices), max_fail_details) if max_fail_details > 0 else len(fail_indices)
                
                for idx in fail_indices[:limit]:
                    file_name = str(vdf.loc[idx, "file"]) if "file" in vdf.columns else ""
                    val = numeric_data.loc[idx]
                    if pd.isna(val):
                        reason = "nan"
                    else:
                        reason = f"out_of_range:{val} not in [{mn_f},{mx_f}]"
                    fail_details.append((model, file_name, f, reason))

        # 写回到 rdf（优化：使用列表解析一次性构建）
        stats_map = {f: (pc, fc, rt) for f, pc, fc, rt in field_stats}
        
        # 优化：使用向量化操作填充统计列
        field_to_stats = rdf["field"].astype(str).map(stats_map)
        rdf["pass_count"] = field_to_stats.apply(lambda x: x[0] if x is not None else None)
        rdf["fail_count"] = field_to_stats.apply(lambda x: x[1] if x is not None else None)
        rdf["pass_rate"] = field_to_stats.apply(lambda x: x[2] if x is not None else None)

        # 输出路径：默认覆盖 ranges_csv
        if output_ranges_csv is None:
            out_csv = Path(ranges_csv)
        else:
            # 若给了固定路径：按 model 分文件写（避免互相覆盖）
            out_csv = Path(str(output_ranges_csv)).with_name(f"{model}_{variant}_ranges_with_passrate.csv")

        out_csv.parent.mkdir(parents=True, exist_ok=True)
        rdf.to_csv(out_csv, index=False, encoding="utf-8-sig")
        out_map[model] = out_csv

        # 打印 TopN
        if show_top_fields:
            # 按 fail_count 降序
            tmp = [(f, pc, fc, rt) for f, pc, fc, rt in field_stats]
            tmp.sort(key=lambda x: (x[2], -x[3] if x[3] is not None else 0), reverse=True)
            top = tmp[: max(0, top_n_fields)]
            _log(logger, log_mode, f"[test_range:{variant}] model={model} Top{top_n_fields} fail fields:")
            for f, pc, fc, rt in top:
                _log(logger, log_mode, f"  - {f}: pass={pc} fail={fc} pass_rate={rt:.4f}")

        # 打印失败明细（限制行数）
        if show_fail_details and fail_details:
            _log(logger, log_mode, f"[test_range:{variant}] model={model} fail details (max={max_fail_details}):")
            shown = 0
            for item in fail_details:
                if max_fail_details > 0 and shown >= max_fail_details:
                    break
                _, file_name, field, reason = item
                _log(logger, log_mode, f"  FAIL file={file_name} field={field} reason={reason}")
                shown += 1

        _log(logger, log_mode, f"[test_range:{variant}] model={model} 写回 ranges -> {out_csv}")

        # 计算构型总通过率（每个 json 所有字段都符合才算通过）
        model_pass_count = 0
        model_total_count = len(vdf)
        
        for idx in vdf.index:
            episode_pass = True  # 假设该 episode 通过
            
            for f in fields:
                if f not in ranges_map:
                    if missing_range_is_fail:
                        episode_pass = False
                        break
                    else:
                        continue
                
                mn, mx, non_numeric = ranges_map[f]
                
                # 字段不在 values 中
                if f not in vdf.columns:
                    if missing_value_col_is_fail:
                        episode_pass = False
                        break
                    else:
                        continue
                
                value = vdf.loc[idx, f]
                
                # 非数值字段检测
                if non_numeric or (str(mn) == "true" and str(mx) == "true"):
                    if pd.isna(value) or str(value).strip() == "" or str(value).lower() == "nan":
                        episode_pass = False
                        break
                    continue
                
                # 数值字段检测
                try:
                    mn_f = float(str(mn).strip())
                    mx_f = float(str(mx).strip())
                except Exception:
                    episode_pass = False
                    break
                
                numeric_value = pd.to_numeric(value, errors="coerce")
                
                if treat_nan_as_fail:
                    if pd.isna(numeric_value) or not (mn_f <= numeric_value <= mx_f):
                        episode_pass = False
                        break
                else:
                    # NaN 不算失败，但超出范围算失败
                    if pd.notna(numeric_value) and not (mn_f <= numeric_value <= mx_f):
                        episode_pass = False
                        break
            
            if episode_pass:
                model_pass_count += 1
        
        model_pass_rate = model_pass_count / model_total_count if model_total_count > 0 else 0.0
        _log(logger, log_mode, f"[test_range:{variant}] ========================================")
        _log(logger, log_mode, f"[test_range:{variant}] 构型 {model} 总通过率: {model_pass_rate:.4f} ({model_pass_count}/{model_total_count})")
        _log(logger, log_mode, f"[test_range:{variant}] ========================================")

        # Episode 通过率统计
        if enable_episode_passrate:
            episode_stats = _calculate_episode_passrate(
                vdf=vdf,
                ranges_map=ranges_map,
                fields=fields,
                treat_nan_as_fail=treat_nan_as_fail,
            )
            
            if episode_stats:
                # 输出 CSV
                ep_csv_name = episode_passrate_filename.format(model=model)
                ep_csv = out_csv.parent / ep_csv_name
                ep_df = pd.DataFrame(episode_stats, columns=["file", "total_fields", "pass_fields", "fail_fields", "pass_rate"])
                ep_df.to_csv(ep_csv, index=False, encoding="utf-8-sig")
                _log(logger, log_mode, f"[test_range:{variant}] model={model} episode passrate -> {ep_csv}")
                
                # 打印 TopN 失败 episodes
                if show_top_failed_episodes:
                    # 按 fail_fields 降序排序
                    sorted_stats = sorted(episode_stats, key=lambda x: (x[3], -x[4]), reverse=True)
                    top_failed = sorted_stats[:top_n_failed_episodes]
                    _log(logger, log_mode, f"[test_range:{variant}] model={model} Top{top_n_failed_episodes} failed episodes:")
                    for file_name, total, passed, failed, rate in top_failed:
                        _log(logger, log_mode, f"  - {file_name}: total={total} pass={passed} fail={failed} pass_rate={rate:.4f}")

    return out_map


def _calculate_episode_passrate(
    vdf: pd.DataFrame,
    ranges_map: Dict[str, Tuple[Any, Any, bool]],
    fields: List[str],
    treat_nan_as_fail: bool,
) -> List[Tuple[str, int, int, int, float]]:
    """
    计算每条 episode 的总体通过率
    返回：[(file, total_fields, pass_fields, fail_fields, pass_rate), ...]
    """
    if vdf.empty:
        return []
    
    episode_stats = []
    file_col = "file" if "file" in vdf.columns else None
    
    for idx in vdf.index:
        file_name = str(vdf.loc[idx, file_col]) if file_col else f"row_{idx}"
        
        total_fields = 0
        pass_fields = 0
        
        for field in fields:
            if field not in ranges_map:
                continue
            if field not in vdf.columns:
                # 缺失字段算失败
                total_fields += 1
                continue
            
            mn, mx, non_numeric = ranges_map[field]
            value = vdf.loc[idx, field]
            
            # 非数值字段检测
            if non_numeric or (str(mn) == "true" and str(mx) == "true"):
                # 检查非空
                if pd.notna(value) and str(value).strip() != "" and str(value).lower() != "nan":
                    pass_fields += 1
                total_fields += 1
                continue
            
            # 数值字段检测
            try:
                mn_f = float(str(mn).strip())
                mx_f = float(str(mx).strip())
            except Exception:
                # 无法解析范围，算失败
                total_fields += 1
                continue
            
            numeric_value = pd.to_numeric(value, errors="coerce")
            
            if treat_nan_as_fail:
                # NaN 算失败
                if pd.notna(numeric_value) and mn_f <= numeric_value <= mx_f:
                    pass_fields += 1
                total_fields += 1
            else:
                # NaN 不计入统计
                if pd.notna(numeric_value):
                    if mn_f <= numeric_value <= mx_f:
                        pass_fields += 1
                    total_fields += 1
        
        fail_fields = total_fields - pass_fields
        pass_rate = pass_fields / total_fields if total_fields > 0 else 0.0
        
        episode_stats.append((file_name, total_fields, pass_fields, fail_fields, pass_rate))
    
    return episode_stats


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() == "nan":
        return float("nan")
    try:
        return float(s)
    except Exception:
        return float("nan")


def _log(logger, log_mode: str, msg: str) -> None:
    if logger is None:
        print(msg)
        return
    logger.info(msg)
