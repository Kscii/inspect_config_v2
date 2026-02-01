# src/steps/find_range_full.py
# -*- coding: utf-8 -*-
"""
find_range_full step

语义（按你最终确认的版本）：
1) 从 {model}.csv 获取字段全集（除 file）
2) 非数值字段：必须命中 force_non_numeric_field_rules -> 输出 true,true（non_numeric_mark）
3) 其余字段视为“数值字段”，必须且仅能命中 1 条 field_range_rules（contains + range:[lo,hi]）
4) 冲突检测：
   - 数值字段命中 >=2 条 range 规则 -> 报错终止（指出 field + 命中的规则）
   - 数值字段命中 0 条 range 规则 -> 报错终止（指出 field）
   - 非数值字段若命中任何 range 规则 -> 报错终止（避免同一字段既被 force 又被 range 定义）
5) 禁止：
   - field_range_rules / force_non_numeric_field_rules 中出现 models/model
   - field_range_rules 使用旧字段 min_lo/max_lo/min_hi/max_hi
   - range 允许 null / lo>hi
6) 输出：repo_root/csv_output/<model>/<model>_ranges_full.csv （列：field,min,max）
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import numpy as np


@dataclass
class FindRangeFullResult:
    model_to_ranges_full_csv: Dict[str, Path]


# ----------------------------
# 匹配语义：沿用 find_range 的 contains-all-in-field 逻辑
# ----------------------------

def _match_rule(field_name: str, contains_list: List[str], case_insensitive: bool) -> bool:
    if not contains_list:
        return False
    if case_insensitive:
        fn = field_name.lower()
        return all((s or "").lower() in fn for s in contains_list if s)
    return all((s or "") in field_name for s in contains_list if s)


def _match_any_contains(
    field_name: str,
    rules: List[Dict[str, Any]],
    field_case_insensitive: bool,
) -> bool:
    for r in rules or []:
        if not isinstance(r, dict):
            continue
        contains = r.get("contains") or []
        if _match_rule(field_name, list(contains), field_case_insensitive):
            return True
    return False


def _find_all_matching_rules(
    field_name: str,
    rules: List[Dict[str, Any]],
    field_case_insensitive: bool,
) -> List[Tuple[int, Dict[str, Any]]]:
    matched: List[Tuple[int, Dict[str, Any]]] = []
    for i, r in enumerate(rules or []):
        if not isinstance(r, dict):
            continue
        contains = r.get("contains") or []
        if _match_rule(field_name, list(contains), field_case_insensitive):
            matched.append((i, r))
    return matched


def _to_float_strict(x: Any, *, ctx: str) -> float:
    try:
        v = float(x)
    except Exception as e:
        raise ValueError(f"[find_range_full] {ctx} 不能转 float: {x!r}") from e
    if not np.isfinite(v):
        raise ValueError(f"[find_range_full] {ctx} 必须是 finite float: {x!r}")
    return float(v)


def _validate_rules_strict(step_cfg: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]], bool]:
    """
    全局强校验：
    - 禁止 models/model
    - field_range_rules 必须含 range:[lo,hi] 且 lo<=hi 且不可为 null
    - 禁止旧字段 min_lo/max_lo/min_hi/max_hi
    """
    non_numeric_mark: str = str(step_cfg.get("non_numeric_mark", "true"))

    force_rules = list(step_cfg.get("force_non_numeric_field_rules") or [])
    range_rules = list(step_cfg.get("field_range_rules") or [])
    case_insensitive: bool = bool(step_cfg.get("field_rule_case_insensitive", True))

    forbidden_model_keys = {"models", "model"}
    forbidden_old_keys = {"min_lo", "max_lo", "min_hi", "max_hi"}

    # force rules：只允许 contains（以及未来可能扩展的其它非 model 字段）
    for i, r in enumerate(force_rules):
        if not isinstance(r, dict):
            raise ValueError(f"[find_range_full] force_non_numeric_field_rules[{i}] 必须是 dict")
        if any(k in r for k in forbidden_model_keys):
            raise ValueError(f"[find_range_full] force_non_numeric_field_rules[{i}] 禁止出现 models/model: keys={list(r.keys())}")
        if "contains" not in r:
            raise ValueError(f"[find_range_full] force_non_numeric_field_rules[{i}] 缺少 contains")

    # range rules：必须 contains + range
    for i, r in enumerate(range_rules):
        if not isinstance(r, dict):
            raise ValueError(f"[find_range_full] field_range_rules[{i}] 必须是 dict")
        if any(k in r for k in forbidden_model_keys):
            raise ValueError(f"[find_range_full] field_range_rules[{i}] 禁止出现 models/model: keys={list(r.keys())}")
        if any(k in r for k in forbidden_old_keys):
            raise ValueError(f"[find_range_full] field_range_rules[{i}] 禁止旧字段 min_lo/max_lo/min_hi/max_hi: keys={list(r.keys())}")

        if "contains" not in r:
            raise ValueError(f"[find_range_full] field_range_rules[{i}] 缺少 contains")
        if "range" not in r:
            raise ValueError(f"[find_range_full] field_range_rules[{i}] 缺少 range:[lo,hi]")

        rr = r.get("range")
        if not isinstance(rr, (list, tuple)) or len(rr) != 2:
            raise ValueError(f"[find_range_full] field_range_rules[{i}].range 必须是长度=2的 list/tuple, got={rr!r}")

        lo = rr[0]
        hi = rr[1]
        if lo is None or hi is None:
            raise ValueError(f"[find_range_full] field_range_rules[{i}].range 不允许 null: {rr!r}")

        lo_f = _to_float_strict(lo, ctx=f"field_range_rules[{i}].range[0]")
        hi_f = _to_float_strict(hi, ctx=f"field_range_rules[{i}].range[1]")
        if lo_f > hi_f:
            raise ValueError(f"[find_range_full] field_range_rules[{i}] lo>hi 不允许: lo={lo_f} hi={hi_f} contains={r.get('contains')}")

    return non_numeric_mark, force_rules, range_rules, case_insensitive


def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> FindRangeFullResult:
    logger = runtime.get("logger")
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode"))

    models: List[str] = runtime["models"]
    model_to_values_csv: Dict[str, Path] = runtime.get("model_to_values_csv", {}) or {}

    non_numeric_mark, force_rules, range_rules, case_insensitive = _validate_rules_strict(step_cfg)

    out_map: Dict[str, Path] = {}

    for model in models:
        values_csv = model_to_values_csv.get(model)
        if not values_csv:
            values_csv = repo_root / "csv_output" / model / f"{model}.csv"
        if not values_csv or not Path(values_csv).exists():
            _log(logger, log_mode, f"[find_range_full] model={model} 缺少 values.csv -> 跳过")
            continue

        vdf = pd.read_csv(values_csv, encoding="utf-8-sig")
        if vdf.empty:
            _log(logger, log_mode, f"[find_range_full] model={model} values.csv 为空 -> 跳过")
            continue

        fields = [c for c in vdf.columns if c != "episode_id"]
        rows: List[Dict[str, Any]] = []

        conflicts: List[str] = []
        missings: List[str] = []

        for field in fields:
            # 1) 非数值字段：必须由 force rules 定义
            is_force_non_numeric = _match_any_contains(field, force_rules, case_insensitive)

            matched = _find_all_matching_rules(field, range_rules, case_insensitive)

            if is_force_non_numeric:
                # 非数值字段：不允许同时命中任何 range rule（防止双重定义）
                if matched:
                    detail = _format_rule_hits(field, matched)
                    conflicts.append(f"[NON_NUMERIC_COLLISION] field={field} hit_range_rules={detail}")
                    continue
                rows.append({"field": field, "min": non_numeric_mark, "max": non_numeric_mark})
                continue

            # 2) 数值字段：必须且仅能命中 1 条 range rule
            if len(matched) == 0:
                missings.append(field)
                continue
            if len(matched) >= 2:
                detail = _format_rule_hits(field, matched)
                conflicts.append(f"[MULTI_RANGE_RULES] field={field} hits={detail}")
                continue

            idx, rule = matched[0]
            rr = rule.get("range")
            lo_f = _to_float_strict(rr[0], ctx=f"field_range_rules[{idx}].range[0] field={field}")
            hi_f = _to_float_strict(rr[1], ctx=f"field_range_rules[{idx}].range[1] field={field}")
            if lo_f > hi_f:
                conflicts.append(f"[LO_GT_HI] field={field} rule_index={idx} lo={lo_f} hi={hi_f} contains={rule.get('contains')}")
                continue

            rows.append({"field": field, "min": lo_f, "max": hi_f})

        # 3) 汇总错误并终止（不删除产物）
        if conflicts or missings:
            if conflicts:
                _log(logger, log_mode, f"[find_range_full][ERROR] model={model} 发现冲突字段={len(conflicts)}")
                for line in conflicts:
                    _log(logger, log_mode, f"  {line}")
            if missings:
                _log(logger, log_mode, f"[find_range_full][ERROR] model={model} 发现未定义字段={len(missings)}")
                for f in missings[:50]:
                    _log(logger, log_mode, f"  [MISSING_RULE] field={f}")
                if len(missings) > 50:
                    _log(logger, log_mode, f"  ... and {len(missings)-50} more")

            # 终止：让 pipeline 捕获并写 exception
            raise ValueError(f"[find_range_full] model={model} conflict={len(conflicts)} missing={len(missings)}")

        out_dir = repo_root / "csv_output" / model
        out_dir.mkdir(parents=True, exist_ok=True)
        out_csv = out_dir / f"{model}_ranges_full.csv"

        out_df = pd.DataFrame(rows, columns=["field", "min", "max"])
        out_df.to_csv(out_csv, index=False, encoding="utf-8-sig")

        _log(logger, log_mode, f"[find_range_full] model={model} ranges_full={len(rows)} -> {out_csv}")
        out_map[model] = out_csv

    return FindRangeFullResult(model_to_ranges_full_csv=out_map)


def _format_rule_hits(field: str, hits: List[Tuple[int, Dict[str, Any]]]) -> str:
    parts = []
    for idx, r in hits:
        contains = r.get("contains")
        rr = r.get("range")
        parts.append(f"#{idx} contains={contains} range={rr}")
    return "; ".join(parts)


def _log(logger, log_mode: str, msg: str) -> None:
    if logger is None:
        print(msg)
        return
    logger.info(msg)
