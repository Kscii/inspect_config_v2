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
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode"))

    models: List[str] = runtime["models"]
    model_to_values_csv: Dict[str, Path] = runtime.get("model_to_values_csv", {}) or {}

    method: str = str(step_cfg.get("method", "cover")).lower()
    iqr_k: float = float(step_cfg.get("iqr_k"))
    mad_z: float = float(step_cfg.get("mad_z"))
    cover_pct: float = float(step_cfg.get("cover_pct"))

    min_valid_count: int = int(step_cfg.get("min_valid_count"))
    clamp_to_observed: bool = bool(step_cfg.get("clamp_to_observed"))
    round_decimals = step_cfg.get("round_decimals")
    round_decimals = None if round_decimals is None else int(round_decimals)

    expand_pct: float = float(step_cfg.get("expand_pct"))

    step_order: List[str] = list(step_cfg.get("step_order"))
    step_order = _validate_step_order(step_order)

    sign_consistency_clamp: bool = bool(step_cfg.get("sign_consistency_clamp"))
    post_expand_sign_clamp: bool = bool(step_cfg.get("post_expand_sign_clamp"))

    non_numeric_mark: str = str(step_cfg.get("non_numeric_mark"))

    force_non_numeric_field_rules: List[Dict[str, Any]] = list(step_cfg.get("force_non_numeric_field_rules"))
    field_range_rules: List[Dict[str, Any]] = list(step_cfg.get("field_range_rules"))

    field_rule_case_insensitive: bool = bool(step_cfg.get("field_rule_case_insensitive"))
    field_rule_model_case_insensitive: bool = bool(step_cfg.get("field_rule_model_case_insensitive"))
    apply_all_matching_rules: bool = bool(step_cfg.get("apply_all_matching_rules"))

    out_map: Dict[str, Path] = {}

    for model in models:
        values_csv = model_to_values_csv.get(model)
        if not values_csv:
            values_csv = repo_root / "csv_output" / model / f"{model}.csv"
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
            raw = df[field]

            # 1) force non-numeric
            if _match_any_contains(
                field_name=field,
                rules=force_non_numeric_field_rules,
                current_model=model,
                field_case_insensitive=field_rule_case_insensitive,
                model_case_insensitive=field_rule_model_case_insensitive,
            ):
                rows.append(_row_non_numeric(field, non_numeric_mark))
                continue

            # 2) 数值解析
            s_num = pd.to_numeric(raw, errors="coerce")
            s = s_num.dropna()
            n = int(s.shape[0])

            # n==0：写 true/true
            if n == 0:
                rows.append(_row_non_numeric(field, non_numeric_mark))
                continue

            min_obs = float(s.min())
            max_obs = float(s.max())

            # 3) 初始区间
            if n < int(min_valid_count):
                lo, hi = min_obs, max_obs
            else:
                lo, hi = _calc_range_old(
                    s=s,
                    method=method,
                    iqr_k=iqr_k,
                    mad_z=mad_z,
                    cover_pct=cover_pct,
                    min_obs=min_obs,
                    max_obs=max_obs,
                )

            lo = float(lo)
            hi = float(hi)

            # 4) 后处理：严格按 step_order，且 clamp_to_observed 在 expand step 内
            for step in step_order:
                if step == "sign":
                    if sign_consistency_clamp:
                        lo, hi = _apply_sign_consistency_constraints(lo, hi, s, min_obs, max_obs)

                elif step == "expand":
                    lo, hi = _expand_interval(lo, hi, expand_pct)

                    if post_expand_sign_clamp:
                        lo, hi = _apply_post_expand_sign_clamp(lo, hi, s)

                    if clamp_to_observed:
                        lo = max(lo, min_obs)
                        hi = min(hi, max_obs)
                        if lo > hi:
                            hi = lo

                elif step == "field":
                    lo, hi = _apply_field_range_rules(
                        field_name=field,
                        lo=lo,
                        hi=hi,
                        rules=field_range_rules,
                        current_model=model,
                        field_case_insensitive=field_rule_case_insensitive,
                        model_case_insensitive=field_rule_model_case_insensitive,
                        apply_all=apply_all_matching_rules,
                    )

                else:
                    # _validate_step_order 已保证不会走到这里
                    raise ValueError(f"Unknown step in step_order: {step}")

            # 5) round
            if round_decimals is not None:
                lo = round(float(lo), int(round_decimals))
                hi = round(float(hi), int(round_decimals))

            rows.append({"field": field, "min": lo, "max": hi})

        out_dir = repo_root / "csv_output" / model
        out_dir.mkdir(parents=True, exist_ok=True)
        out_csv = out_dir / f"{model}_ranges.csv"

        out_df = pd.DataFrame(rows, columns=["field", "min", "max"])
        out_df.to_csv(out_csv, index=False, encoding="utf-8-sig")

        _log(logger, log_mode, f"[find_range] model={model} ranges={len(rows)} -> {out_csv}")
        out_map[model] = out_csv

    return FindRangeResult(model_to_ranges_csv=out_map)


# ----------------------------
# 旧版算法与规则实现
# ----------------------------

def _calc_range_old(
    s: pd.Series,
    method: str,
    iqr_k: float,
    mad_z: float,
    cover_pct: float,
    min_obs: float,
    max_obs: float,
) -> Tuple[float, float]:
    if method == "iqr":
        q1 = float(s.quantile(0.25))
        q3 = float(s.quantile(0.75))
        iqr = q3 - q1
        if iqr == 0.0 or (not np.isfinite(iqr)):
            return float(min_obs), float(max_obs)
        lo = q1 - float(iqr_k) * iqr
        hi = q3 + float(iqr_k) * iqr
        return float(lo), float(hi)

    if method == "mad":
        med = float(s.median())
        mad = float((s - med).abs().median())
        if mad == 0.0 or (not np.isfinite(mad)):
            return float(min_obs), float(max_obs)
        sigma = 1.4826 * mad
        lo = med - float(mad_z) * sigma
        hi = med + float(mad_z) * sigma
        return float(lo), float(hi)

    if method == "cover":
        vals = np.sort(s.to_numpy(dtype=float))
        lo, hi = _shortest_cover_interval(vals, float(cover_pct))
        return float(lo), float(hi)

    raise ValueError(f"Unknown method: {method}")


def _shortest_cover_interval(sorted_vals: np.ndarray, cover_pct: float) -> Tuple[float, float]:
    n = int(sorted_vals.size)
    if n <= 0:
        return (float("nan"), float("nan"))

    p = float(cover_pct)
    if not np.isfinite(p):
        raise ValueError(f"cover_pct must be finite, got: {cover_pct}")
    p = max(min(p, 1.0), 0.0)

    if p <= 0.0:
        v = float(sorted_vals[0])
        return (v, v)

    k = int(math.ceil(p * n))
    k = max(1, min(k, n))

    if k == 1:
        v = float(sorted_vals[n // 2])
        return (v, v)

    best_i = 0
    best_w = float("inf")
    for i in range(0, n - k + 1):
        w = float(sorted_vals[i + k - 1] - sorted_vals[i])
        if w < best_w:
            best_w = w
            best_i = i

    lo = float(sorted_vals[best_i])
    hi = float(sorted_vals[best_i + k - 1])
    return (lo, hi)


def _expand_interval(lo: float, hi: float, expand_pct: float) -> Tuple[float, float]:
    """
    旧版语义：
    - 按区间宽度比例扩张：d = w * p
    - w==0 时用 scale fallback：scale=max(|lo|,|hi|,1) -> d=scale*p
    - p 可为负数（收缩）
    - 保底避免 lo>hi：压到中点
    """
    p = float(expand_pct)
    if (not np.isfinite(p)) or p == 0.0:
        return float(lo), float(hi)

    lo = float(lo)
    hi = float(hi)

    w = hi - lo
    if np.isfinite(w) and w != 0.0:
        d = w * p
        lo2 = lo - d
        hi2 = hi + d
    else:
        scale = max(abs(lo), abs(hi), 1.0)
        d = scale * p
        lo2 = lo - d
        hi2 = hi + d

    if lo2 > hi2:
        mid = (lo2 + hi2) / 2.0
        lo2 = mid
        hi2 = mid

    return float(lo2), float(hi2)


def _apply_sign_consistency_constraints(lo: float, hi: float, s: pd.Series, min_obs: float, max_obs: float) -> Tuple[float, float]:
    """
    旧版 sign（增强版）：
    - 若所有值 >= 0：lo = min_obs
    - 若所有值 <= 0：hi = max_obs
    """
    if s.empty:
        return float(lo), float(hi)

    mn = float(s.min())
    mx = float(s.max())

    lo = float(lo)
    hi = float(hi)

    if mn >= 0.0:
        lo = float(min_obs)
    if mx <= 0.0:
        hi = float(max_obs)

    if lo > hi:
        hi = lo
    return float(lo), float(hi)


def _apply_post_expand_sign_clamp(lo: float, hi: float, s: pd.Series) -> Tuple[float, float]:
    """
    旧版扩张后同号保底：
    - 若所有值 >= 0：lo >= 0
    - 若所有值 <= 0：hi <= 0
    """
    if s.empty:
        return float(lo), float(hi)

    mn = float(s.min())
    mx = float(s.max())

    lo = float(lo)
    hi = float(hi)

    if mn >= 0.0:
        lo = max(lo, 0.0)
    if mx <= 0.0:
        hi = min(hi, 0.0)

    if lo > hi:
        hi = lo
    return float(lo), float(hi)


def _validate_step_order(step_order: List[str]) -> List[str]:
    allowed = {"sign", "expand", "field"}
    order = list(step_order or [])
    if set(order) != allowed or len(order) != 3:
        raise ValueError(f"step_order must be a permutation of {sorted(allowed)}, got: {step_order}")
    return order


# ----------------------------
# 字段规则匹配语义
# ----------------------------

def _get_rule_models(rule: Dict[str, Any]) -> List[str]:
    ms = rule.get("models", None)
    if isinstance(ms, (list, tuple)):
        return [str(x).strip() for x in ms if str(x).strip()]
    m = rule.get("model", None)
    if isinstance(m, str) and m.strip():
        return [m.strip()]
    return []


def _match_models(current_model: str, rule: Dict[str, Any], case_insensitive: bool) -> bool:
    models = _get_rule_models(rule)
    if not models:
        return True  # 无 models 时使用通用规则

    cm = (current_model or "").strip()
    if case_insensitive:
        cm2 = cm.lower()
        models2 = [m.lower() for m in models]
        return cm2 in models2
    return cm in models


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
    current_model: str,
    field_case_insensitive: bool,
    model_case_insensitive: bool,
) -> bool:
    for r in rules or []:
        if not isinstance(r, dict):
            continue
        if not _match_models(current_model, r, model_case_insensitive):
            continue
        contains = r.get("contains") or []
        if _match_rule(field_name, list(contains), field_case_insensitive):
            return True
    return False


def _to_finite_float(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except Exception:
        return None
    if not np.isfinite(v):
        return None
    return v


def _is_model_specific_rule(rule: Dict[str, Any]) -> bool:
    # “带 models/model 且非空” 才算 model-specific
    return bool(_get_rule_models(rule))


def _apply_one_range_rule(rule: Dict[str, Any], lo: float, hi: float) -> Tuple[float, float]:
    min_lo = _to_finite_float(rule.get("min_lo", None))
    max_lo = _to_finite_float(rule.get("max_lo", None))
    min_hi = _to_finite_float(rule.get("min_hi", None))
    max_hi = _to_finite_float(rule.get("max_hi", None))

    if min_lo is not None:
        lo = max(lo, float(min_lo))
    if max_lo is not None:
        lo = min(lo, float(max_lo))

    if min_hi is not None:
        hi = max(hi, float(min_hi))
    if max_hi is not None:
        hi = min(hi, float(max_hi))

    return float(lo), float(hi)


def _apply_field_range_rules(
    field_name: str,
    lo: float,
    hi: float,
    rules: List[Dict[str, Any]],
    current_model: str,
    field_case_insensitive: bool,
    model_case_insensitive: bool,
    apply_all: bool,
) -> Tuple[float, float]:
    lo = float(lo)
    hi = float(hi)

    matched_specific = False

    # 1) 先跑 model-specific
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        if not _is_model_specific_rule(rule):
            continue
        if not _match_models(current_model, rule, model_case_insensitive):
            continue

        contains = rule.get("contains") or []
        if not _match_rule(field_name, list(contains), field_case_insensitive):
            continue

        lo, hi = _apply_one_range_rule(rule, lo, hi)
        matched_specific = True
        if not apply_all:
            break

    # 命中过 model-specific：直接返回（不再应用通用规则）
    if matched_specific:
        if lo > hi:
            hi = lo
        return float(lo), float(hi)

    # 2) 否则跑通用规则（无 models/model）
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        if _is_model_specific_rule(rule):
            continue  # 只应用通用部分

        # 通用规则默认 _match_models=True（因为无 models），这里也保持一致
        contains = rule.get("contains") or []
        if not _match_rule(field_name, list(contains), field_case_insensitive):
            continue

        lo, hi = _apply_one_range_rule(rule, lo, hi)
        if not apply_all:
            break

    if lo > hi:
        hi = lo
    return float(lo), float(hi)


def _row_non_numeric(field: str, non_numeric_mark: str) -> Dict[str, Any]:
    return {"field": field, "min": non_numeric_mark, "max": non_numeric_mark}


def _log(logger, log_mode: str, msg: str) -> None:
    if logger is None:
        print(msg)
        return
    logger.info(msg)
