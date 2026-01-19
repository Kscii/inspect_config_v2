# src/steps/collect.py
# -*- coding: utf-8 -*-
"""
collect step
职责：
1) 读取 selectors.txt
2) 遍历 obs_download/<model>/... 的 json 文件
3) 按 selector 提取值，输出 values.csv（单文件或多文件）
"""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass
class CollectResult:
    model_to_values_csv: Dict[str, Path]


def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> CollectResult:
    logger = runtime.get("logger")
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode", "normal"))
    obs_download_root: Path = runtime["obs_download_root"]
    models: List[str] = runtime["models"]
    model_to_selectors_txt: Dict[str, Path] = runtime["model_to_selectors_txt"]

    csv_output_dir = repo_root / "csv_output"
    csv_output_dir.mkdir(parents=True, exist_ok=True)

    single_file_mode: bool = bool(step_cfg.get("single_file_mode", True))
    json_suffix: str = str(step_cfg.get("json_suffix", ".json"))
    csv_encoding: str = str(step_cfg.get("csv_encoding", "utf-8-sig"))
    csv_write_header: bool = bool(step_cfg.get("csv_write_header", True))
    filename_in_csv: str = str(step_cfg.get("filename_in_csv", "basename"))
    strict_mode: bool = bool(step_cfg.get("strict_mode", False))

    model_to_values: Dict[str, Path] = {}

    for model in models:
        model_root = obs_download_root / model
        if not model_root.exists():
            _log(logger, log_mode, f"[collect] model={model} 不存在目录：{model_root} -> 跳过")
            continue

        selectors_txt = model_to_selectors_txt.get(model)
        if not selectors_txt or not selectors_txt.exists():
            _log(logger, log_mode, f"[collect] model={model} 缺少 selectors.txt -> 跳过")
            continue

        selectors = [line.strip() for line in selectors_txt.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not selectors:
            _log(logger, log_mode, f"[collect] model={model} selectors 为空 -> 跳过")
            continue

        json_files = [p for p in model_root.rglob(f"*{json_suffix}") if p.is_file()]
        if not json_files:
            _log(logger, log_mode, f"[collect] model={model} 未找到 json -> 跳过")
            continue

        out_dir = csv_output_dir / model
        out_dir.mkdir(parents=True, exist_ok=True)

        if single_file_mode:
            out_csv = out_dir / f"{model}.csv"
            _write_single_csv(
                model_root=model_root,
                json_files=json_files,
                selectors=selectors,
                out_csv=out_csv,
                encoding=csv_encoding,
                write_header=csv_write_header,
                filename_mode=filename_in_csv,
                strict_mode=strict_mode,
                logger=logger,
                log_mode=log_mode,
            )

            model_to_values[model] = out_csv
        else:
            # 多文件模式：每个 selector 一个 CSV（这里保留能力，但你当前主要用单文件模式）
            # 为避免项目拆散，这里仍放在同一个 step 文件里
            for sel in selectors:
                out_csv = out_dir / _safe_selector_filename(sel)  # 每个 selector 一个文件
                _write_multi_csv_for_selector(
                    model_root=model_root,
                    json_files=json_files,
                    selector=sel,
                    out_csv=out_csv,
                    encoding=csv_encoding,
                    write_header=csv_write_header,
                    filename_mode=filename_in_csv,
                    strict_mode=strict_mode,
                    logger=logger,
                    log_mode=log_mode,
                )
            model_to_values[model] = out_dir / f"{model}.csv"  # 语义上返回一个“主产物”，实际多文件在目录下

        _log(logger, log_mode, f"[collect] model={model} 完成，json_files={len(json_files)}")

    return CollectResult(model_to_values_csv=model_to_values)


# ----------------------------
# 写 CSV（单文件模式）
# ----------------------------

def _write_single_csv(
    model_root: Path,
    json_files: List[Path],
    selectors: List[str],
    out_csv: Path,
    encoding: str,
    write_header: bool,
    filename_mode: str,
    strict_mode: bool,
    logger,
    log_mode: str,
) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    with out_csv.open("w", encoding=encoding, newline="") as f:
        w = csv.writer(f)

        if write_header:
            w.writerow(["file"] + selectors)

        for jf in sorted(json_files):
            try:
                data = json.loads(jf.read_text(encoding="utf-8-sig", errors="ignore"))
                row = [_format_filename(jf, model_root, filename_mode)]
                for sel in selectors:
                    v = _eval_selector(data, sel)
                    row.append(_to_cell(v))
                w.writerow(row)
            except Exception as e:
                msg = f"[collect] 处理失败：{jf} err={e}"
                if strict_mode:
                    raise RuntimeError(msg)
                _log(logger, log_mode, msg)


# ----------------------------
# 多文件模式（每 selector 一个 CSV）
# ----------------------------

def _write_multi_csv_for_selector(
    model_root: Path,
    json_files: List[Path],
    selector: str,
    out_csv: Path,
    encoding: str,
    write_header: bool,
    filename_mode: str,
    strict_mode: bool,
    logger,
    log_mode: str,
) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding=encoding, newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["file", "value"])
        for jf in sorted(json_files):
            try:
                data = json.loads(jf.read_text(encoding="utf-8-sig", errors="ignore"))
                v = _eval_selector(data, selector)
                w.writerow([_format_filename(jf, model_root, filename_mode), _to_cell(v)])
            except Exception as e:
                msg = f"[collect] 处理失败：{jf} selector={selector} err={e}"
                if strict_mode:
                    raise RuntimeError(msg)
                _log(logger, log_mode, msg)


# ----------------------------
# selector 解析与执行（与 selectors step 输出格式配套）
# ----------------------------

# 形如：.<k> 或 .[<k>=<v>]
_SEG_KEY = re.compile(r"\.<([^<>]+)>")
_SEG_FILTER = re.compile(r"\.\[<([^<>]+)>=<([^<>]+)>\]")

def _eval_selector(data: Any, selector: str) -> Any:
    """
    执行 selector：
    - .<key>：dict key
    - .[<k>=<v>]：list[dict] 过滤：选择第一个满足 dict[k]==v 的元素
    """
    s = selector.strip()
    if not s.startswith("."):
        raise ValueError(f"selector must start with '.', got: {selector}")

    i = 0
    cur = data
    while i < len(s):
        if s.startswith(".[", i):
            m = _SEG_FILTER.match(s, i)
            if not m:
                raise ValueError(f"bad filter seg at {i}: {selector}")
            k = m.group(1)
            v = m.group(2)
            cur = _apply_filter(cur, k, v)
            i = m.end()
        elif s.startswith(".<", i):
            m = _SEG_KEY.match(s, i)
            if not m:
                raise ValueError(f"bad key seg at {i}: {selector}")
            key = m.group(1)
            cur = _apply_key(cur, key)
            i = m.end()
        else:
            # 允许出现空字符串前缀（例如 selector 根是 "."）
            if i == 0 and s == ".":
                return cur
            # 不认识的片段
            raise ValueError(f"unknown selector syntax at {i}: {selector}")

    return cur


def _apply_key(cur: Any, key: str) -> Any:
    if isinstance(cur, dict):
        return cur.get(key)
    return None


def _apply_filter(cur: Any, k: str, v: str) -> Any:
    if isinstance(cur, list):
        for it in cur:
            if isinstance(it, dict) and str(it.get(k)) == v:
                return it
        return None
    # 如果不是 list，直接 None
    return None


def _to_cell(v: Any) -> str:
    """
    CSV 单元格：统一转字符串
    - None -> ""
    - dict/list -> json 串（压缩）
    """
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        try:
            return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return str(v)
    return str(v)


def _format_filename(path: Path, model_root: Path, mode: str) -> str:
    if mode == "relative":
        try:
            return str(path.relative_to(model_root)).replace("\\", "/")
        except Exception:
            return path.name
    return path.name


def _safe_selector_filename(selector: str) -> str:
    # 简单转文件名：把特殊字符替换
    s = selector
    for ch in '<>:"/\\|?*[]=':
        s = s.replace(ch, "_")
    s = s.replace(".", "_")
    if len(s) > 120:
        s = s[:120]
    return f"{s}.csv"


def _log(logger, log_mode: str, msg: str) -> None:
    if logger is None:
        print(msg)
        return
    logger.info(msg)
