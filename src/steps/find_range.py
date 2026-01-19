# src/steps/find_range.py
# -*- coding: utf-8 -*-
"""
find_range step
职责：
1) 读取 values.csv（每个 model 一个）
2) 按 METHOD(iqr/mad/cover) 估计区间
3) 按 step_order 做后处理：sign / expand / field
4) 支持 force_non_numeric_field_rules / field_range_rules 等
5) 输出：repo_root/csv_output/<model>/<model>_ranges.csv

变更：
- 不再输出第 4 列 non_numeric，只输出：field,min,max
- 非数值字段仍用 min/max 写入 non_numeric_mark（默认 "true"）
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass
class FindRangeResult:
    model_to_ranges_csv: Dict[str, Path]


def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> FindRangeResult:
    logger = runtime.get("logger")
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode", "normal"))

    models: List[str] = runtime["models"]
    model_to_values_csv: Dict[str, Path] = runtime["model_to_values_csv"]

    method: str = str(step_cfg.get("method", "cover")).lower()
    iqr_k: float = float(step_cfg.get("iqr_k", 1.5))
    mad_z: float = float(step_cfg.get("mad_z", 3.0))
    cover_pct: float = float(step_cfg.get("cover_pct", 0.95))

    min_valid_count: int = int(step_cfg.get("min_valid_count", 2))
    clamp_to_observed: bool = bool(step_cfg.get("clamp_to_observed", True))
    round_decimals = step_cfg.get("round_decimals", 6)
    round_decimals = None if round_decimals is None else int(round_decimals)
    expand_pct: float = float(step_cfg.get("expand_pct", 1))

    step_order: List[str] = list(step_cfg.get("step_order", ["sign", "expand", "field"]))

    sign_consistency_clamp: bool = bool(step_cfg.get("sign_consistency_clamp", True))
    post_expand_sign_clamp: bool = bool(step_cfg.get("post_expand_sign_clamp", True))

    non_numeric_mark: str = str(step_cfg.get("non_numeric_mark", "true"))

    force_non_numeric_field_rules: List[Dict[str, Any]] = list(step_cfg.get("force_non_numeric_field_rules", []))
    field_range_rules: List[Dict[str, Any]] = list(step_cfg.get("field_range_rules", []))

    field_rule_case_insensitive: bool = bool(step_cfg.get("field_rule_case_insensitive", True))
    field_rule_model_case_insensitive: bool = bool(step_cfg.get("field_rule_model_case_insensitive", True))
    apply_all_matching_rules: bool = bool(step_cfg.get("apply_all_matching_rules", True))

    out_map: Dict[str, Path] = {}

    for model in models:
        values_csv = model_to_values_csv.get(model)
        if not values_csv or not Path(values_csv).exists():
            _log(logger, log_mode, f"[find_range] model={model} 缺少 values.csv -> 跳过")
            continue

        df = pd.read_csv(values_csv, encoding="utf-8-sig")
        if df.empty:
            _log(logger, log_mode, f"[find_range] model={model} values.csv 为空 -> 跳过")
            continue

        # 只处理非 file 列
        fields = [c for c in df.columns if c != "file"]
        rows: List[Dict[str, Any]] = []

        for field in fields:
            # 1) force non-numeric
            if _match_force_non_numeric(
                field,
                model,
                force_non_numeric_field_rules,
                field_rule_case_insensitive,
                field_rule_model_case_insensitive,
            ):
                rows.append(_row_non_numeric(field, non_numeric_mark))
                continue

            # 2) 取数值列
            series = df[field]
            nums = _to_float_list(series)
            nums = [x for x in nums if x is not None and not math.isnan(x)]
            if len(nums) < min_valid_count:
                rows.append(_row_non_numeric(field, non_numeric_mark))
                continue

            observed_min = float(np.min(nums))
            observed_max = float(np.max(nums))

            # 3) 算初始区间
            lo, hi = _calc_range(nums, method=method, iqr_k=iqr_k, mad_z=mad_z, cover_pct=cover_pct)

            # 4) 后处理：按 step_order 执行
            for step in step_order:
                if step == "sign":
                    if sign_consistency_clamp:
                        lo, hi = _sign_clamp(lo, hi, nums)
                elif step == "expand":
                    lo, hi = _expand(lo, hi, expand_pct)
                    if post_expand_sign_clamp:
                        lo, hi = _sign_clamp(lo, hi, nums)
                elif step == "field":
                    lo, hi = _apply_field_rules(
                        field=field,
                        model=model,
                        lo=lo,
                        hi=hi,
                        rules=field_range_rules,
                        field_case_insensitive=field_rule_case_insensitive,
                        model_case_insensitive=field_rule_model_case_insensitive,
                        apply_all=apply_all_matching_rules,
                    )
                else:
                    # 未知 step 忽略（保持兼容）
                    pass

            # 5) clamp 到观测范围
            if clamp_to_observed:
                lo = max(lo, observed_min)
                hi = min(hi, observed_max)

            # 6) round
            if round_decimals is not None:
                lo = round(lo, round_decimals)
                hi = round(hi, round_decimals)

            # ✅ 不再输出 non_numeric 列
            rows.append({"field": field, "min": lo, "max": hi})

        out_dir = repo_root / "csv_output" / model
        out_dir.mkdir(parents=True, exist_ok=True)
        out_csv = out_dir / f"{model}_ranges.csv"

        # ✅ 只写 3 列
        out_df = pd.DataFrame(rows, columns=["field", "min", "max"])
        out_df.to_csv(out_csv, index=False, encoding="utf-8-sig")

        _log(logger, log_mode, f"[find_range] model={model} ranges={len(rows)} -> {out_csv}")
        out_map[model] = out_csv

    return FindRangeResult(model_to_ranges_csv=out_map)


# ----------------------------
# 算法与规则工具
# ----------------------------

def _to_float_list(series: pd.Series) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    for v in series.tolist():
        if v is None:
            out.append(None)
            continue
        s = str(v).strip()
        if s == "":
            out.append(None)
            continue
        try:
            out.append(float(s))
        except Exception:
            out.append(None)
    return out


def _calc_range(nums: List[float], method: str, iqr_k: float, mad_z: float, cover_pct: float) -> Tuple[float, float]:
    arr = np.array(nums, dtype=float)

    if method == "iqr":
        q1 = np.percentile(arr, 25)
        q3 = np.percentile(arr, 75)
        iqr = q3 - q1
        lo = q1 - iqr_k * iqr
        hi = q3 + iqr_k * iqr
        return float(lo), float(hi)

    if method == "mad":
        med = np.median(arr)
        mad = np.median(np.abs(arr - med))
        if mad == 0:
            return float(np.min(arr)), float(np.max(arr))
        lo = med - mad_z * mad
        hi = med + mad_z * mad
        return float(lo), float(hi)

    cover_pct = max(0.0, min(1.0, cover_pct))
    tail = (1.0 - cover_pct) / 2.0
    lo = np.percentile(arr, 100 * tail)
    hi = np.percentile(arr, 100 * (1.0 - tail))
    return float(lo), float(hi)


def _expand(lo: float, hi: float, expand_pct: float) -> Tuple[float, float]:
    w = hi - lo
    if w < 0:
        lo, hi = hi, lo
        w = hi - lo

    # 兼容：<=1 视为比例；>1 视为百分比
    if expand_pct <= 1:
        pad = w * expand_pct
    else:
        pad = w * (expand_pct / 100.0)
    return lo - pad, hi + pad


def _sign_clamp(lo: float, hi: float, nums: List[float]) -> Tuple[float, float]:
    all_nonneg = all(x >= 0 for x in nums)
    all_nonpos = all(x <= 0 for x in nums)
    if all_nonneg:
        lo = max(lo, 0.0)
    if all_nonpos:
        hi = min(hi, 0.0)
    return lo, hi


def _match_force_non_numeric(
    field: str,
    model: str,
    rules: List[Dict[str, Any]],
    field_case_insensitive: bool,
    model_case_insensitive: bool,
) -> bool:
    f = field.lower() if field_case_insensitive else field
    m = model.lower() if model_case_insensitive else model

    for r in rules:
        contains = r.get("contains", [])
        models = r.get("models") or r.get("model")
        if models:
            ms = [x.lower() if model_case_insensitive else x for x in models]
            if m not in ms:
                continue
        ok = True
        for c in contains:
            c2 = c.lower() if field_case_insensitive else c
            if c2 not in f:
                ok = False
                break
        if ok:
            return True
    return False


def _apply_field_rules(
    field: str,
    model: str,
    lo: float,
    hi: float,
    rules: List[Dict[str, Any]],
    field_case_insensitive: bool,
    model_case_insensitive: bool,
    apply_all: bool,
) -> Tuple[float, float]:
    f = field.lower() if field_case_insensitive else field
    m = model.lower() if model_case_insensitive else model

    def apply_one(rule: Dict[str, Any], lo0: float, hi0: float) -> Tuple[float, float]:
        lo2, hi2 = lo0, hi0
        if "min_lo" in rule:
            lo2 = max(lo2, float(rule["min_lo"]))
        if "max_lo" in rule:
            lo2 = min(lo2, float(rule["max_lo"]))
        if "min_hi" in rule:
            hi2 = max(hi2, float(rule["min_hi"]))
        if "max_hi" in rule:
            hi2 = min(hi2, float(rule["max_hi"]))
        return lo2, hi2

    matched = False
    for r in rules:
        contains = r.get("contains", [])
        models = r.get("models") or r.get("model")
        if models:
            ms = [x.lower() if model_case_insensitive else x for x in models]
            if m not in ms:
                continue

        ok = True
        for c in contains:
            c2 = c.lower() if field_case_insensitive else c
            if c2 not in f:
                ok = False
                break
        if not ok:
            continue

        lo, hi = apply_one(r, lo, hi)
        matched = True
        if matched and (not apply_all):
            break

    return lo, hi


def _row_non_numeric(field: str, non_numeric_mark: str) -> Dict[str, Any]:
    # ✅ 不再输出 non_numeric 列：用 min/max=mark 表示
    return {"field": field, "min": non_numeric_mark, "max": non_numeric_mark}


def _log(logger, log_mode: str, msg: str) -> None:
    if logger is None:
        print(msg)
        return
    logger.info(msg)
