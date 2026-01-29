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
    model_to_values_csv: Dict[str, Path] = runtime["model_to_values_csv"]

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
                # 非数值字段：这里只验证“非空”
                pass_cnt = 0
                fail_cnt = 0
                for _, r in vdf.iterrows():
                    val = r.get(f)
                    ok = (val is not None) and (str(val).strip() != "") and (str(val).lower() != "nan")
                    if ok:
                        pass_cnt += 1
                    else:
                        fail_cnt += 1
                        if show_fail_details:
                            fail_details.append((model, str(r.get("file", "")), f, "empty"))
                rate = pass_cnt / (pass_cnt + fail_cnt) if (pass_cnt + fail_cnt) else 0.0
                field_stats.append((f, pass_cnt, fail_cnt, rate))
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

            pass_cnt = 0
            fail_cnt = 0
            for _, r in vdf.iterrows():
                raw = r.get(f)
                x = _to_float(raw)
                if x is None or (isinstance(x, float) and math.isnan(x)):
                    if treat_nan_as_fail:
                        fail_cnt += 1
                        if show_fail_details:
                            fail_details.append((model, str(r.get("file", "")), f, "nan"))
                    else:
                        # nan 不计入统计
                        pass
                    continue
                ok = (mn_f <= x <= mx_f)
                if ok:
                    pass_cnt += 1
                else:
                    fail_cnt += 1
                    if show_fail_details:
                        fail_details.append((model, str(r.get("file", "")), f, f"out_of_range:{x} not in [{mn_f},{mx_f}]"))
            total = pass_cnt + fail_cnt
            rate = pass_cnt / total if total else 0.0
            field_stats.append((f, pass_cnt, fail_cnt, rate))

        # 写回到 rdf
        stats_map = {f: (pc, fc, rt) for f, pc, fc, rt in field_stats}
        rdf["pass_count"] = rdf["field"].map(lambda x: stats_map.get(str(x), (None, None, None))[0])
        rdf["fail_count"] = rdf["field"].map(lambda x: stats_map.get(str(x), (None, None, None))[1])
        rdf["pass_rate"] = rdf["field"].map(lambda x: stats_map.get(str(x), (None, None, None))[2])

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

    return out_map


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
