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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


@dataclass
class CollectResult:
    model_to_values_csv: Dict[str, Path]


# ----------------------------
# selector 解析与执行（预编译）
# ----------------------------

# 形如：.<k> 或 .[<k>=<v>]
_SEG_KEY = re.compile(r"\.<([^<>]+)>")
_SEG_FILTER = re.compile(r"\.\[<([^<>]+)>=<([^<>]+)>\]")


# 为了更快：将 selector 编译成操作序列（避免每次 eval 都 regex/scan selector 字符串）
# op 形式：
#   ("K", key)          -> dict key
#   ("F", k, v)         -> list[dict] filter first dict where str(dict[k]) == v
CompiledOp = Union[Tuple[str, str], Tuple[str, str, str]]  # ("K", key) or ("F", k, v)


def _compile_selector(selector: str) -> List[CompiledOp]:
    """
    与原 _eval_selector 支持的语法一致：
    - .<key>：dict key
    - .[<k>=<v>]：list[dict] 过滤：选择第一个满足 dict[k]==v 的元素
    """
    s = selector.strip()
    if not s.startswith("."):
        raise ValueError(f"selector must start with '.', got: {selector}")

    # 允许根是 "."（原行为：返回 data）
    if s == ".":
        return []

    ops: List[CompiledOp] = []
    i = 0
    L = len(s)

    # 仍然用同样的 regex 规则，只是把匹配从“每次取值”挪到“启动时编译一次”
    while i < L:
        if s.startswith(".[", i):
            m = _SEG_FILTER.match(s, i)
            if not m:
                raise ValueError(f"bad filter seg at {i}: {selector}")
            ops.append(("F", m.group(1), m.group(2)))
            i = m.end()
        elif s.startswith(".<", i):
            m = _SEG_KEY.match(s, i)
            if not m:
                raise ValueError(f"bad key seg at {i}: {selector}")
            ops.append(("K", m.group(1)))
            i = m.end()
        else:
            raise ValueError(f"unknown selector syntax at {i}: {selector}")

    return ops


def _eval_compiled(data: Any, ops: List[CompiledOp]) -> Any:
    """
    执行已编译 ops（语义与原 _eval_selector 一致）
    """
    cur = data

    # 用局部变量绑定，减少属性查找开销
    for op in ops:
        t = op[0]
        if t == "K":
            # ("K", key)
            key = op[1]  # type: ignore[index]
            if isinstance(cur, dict):
                cur = cur.get(key)
            else:
                return None
        else:
            # ("F", k, v)
            k = op[1]  # type: ignore[index]
            v = op[2]  # type: ignore[index]
            if isinstance(cur, list):
                hit = None
                for it in cur:
                    if isinstance(it, dict) and str(it.get(k)) == v:
                        hit = it
                        break
                cur = hit
            else:
                return None

    return cur


# ----------------------------
# I/O 与格式化
# ----------------------------

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


def _load_json_text_compat(path: Path) -> Any:
    """
    保持与原行为一致：
    - read_text(encoding="utf-8-sig", errors="ignore")
    - json.loads(...)
    """
    txt = path.read_text(encoding="utf-8-sig", errors="ignore")
    return json.loads(txt)


# ----------------------------
# step 入口
# ----------------------------

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

    # 性能优化：批量 writerows（默认 1024 行一批，不改变输出内容）
    # （新增可选配置，不影响原逻辑；不配时采用默认）
    batch_rows: int = int(step_cfg.get("batch_rows", 1024))
    if batch_rows <= 0:
        batch_rows = 1

    model_to_values: Dict[str, Path] = {}

    # 局部绑定加速
    _exists = Path.exists
    _read_text = Path.read_text
    _splitlines = str.splitlines

    for model in models:
        model_root = obs_download_root / model
        if not _exists(model_root):
            _log(logger, log_mode, f"[collect] model={model} 不存在目录：{model_root} -> 跳过")
            continue

        selectors_txt = model_to_selectors_txt.get(model)
        if not selectors_txt or not _exists(selectors_txt):
            _log(logger, log_mode, f"[collect] model={model} 缺少 selectors.txt -> 跳过")
            continue

        # 读 selectors（与原一致：strip+非空）
        selectors = [
            line.strip()
            for line in _splitlines(_read_text(selectors_txt, encoding="utf-8"))
            if line.strip()
        ]
        if not selectors:
            _log(logger, log_mode, f"[collect] model={model} selectors 为空 -> 跳过")
            continue

        # 保持原行为：收集所有 json 文件并排序
        # 性能点：
        # - rglob 本身是生成器，但为了排序必须落到 list（保持行为）
        # - 仍然尽量减少多余属性查找/调用
        json_files = [p for p in model_root.rglob(f"*{json_suffix}") if p.is_file()]
        if not json_files:
            _log(logger, log_mode, f"[collect] model={model} 未找到 json -> 跳过")
            continue

        out_dir = csv_output_dir / model
        out_dir.mkdir(parents=True, exist_ok=True)

        # 预编译 selectors（核心性能提升点）
        try:
            compiled_selectors: List[Tuple[str, List[CompiledOp]]] = [
                (sel, _compile_selector(sel)) for sel in selectors
            ]
        except Exception as e:
            msg = f"[collect] model={model} 编译 selectors 失败 err={e}"
            if strict_mode:
                raise RuntimeError(msg)
            _log(logger, log_mode, msg)
            continue

        if single_file_mode:
            out_csv = out_dir / f"{model}.csv"
            _write_single_csv(
                model_root=model_root,
                json_files=json_files,
                compiled_selectors=compiled_selectors,
                out_csv=out_csv,
                encoding=csv_encoding,
                write_header=csv_write_header,
                filename_mode=filename_in_csv,
                strict_mode=strict_mode,
                logger=logger,
                log_mode=log_mode,
                batch_rows=batch_rows,
            )
            model_to_values[model] = out_csv
        else:
            # 多文件模式：每个 selector 一个 CSV（行为保持不变）
            for sel, ops in compiled_selectors:
                out_csv = out_dir / _safe_selector_filename(sel)
                _write_multi_csv_for_selector(
                    model_root=model_root,
                    json_files=json_files,
                    selector=sel,
                    compiled_ops=ops,
                    out_csv=out_csv,
                    encoding=csv_encoding,
                    write_header=csv_write_header,
                    filename_mode=filename_in_csv,
                    strict_mode=strict_mode,
                    logger=logger,
                    log_mode=log_mode,
                    batch_rows=batch_rows,
                )
            model_to_values[model] = out_dir / f"{model}.csv"

        _log(logger, log_mode, f"[collect] model={model} 完成，json_files={len(json_files)}")

    return CollectResult(model_to_values_csv=model_to_values)


# ----------------------------
# 写 CSV（单文件模式）
# ----------------------------

def _write_single_csv(
    model_root: Path,
    json_files: List[Path],
    compiled_selectors: List[Tuple[str, List[CompiledOp]]],
    out_csv: Path,
    encoding: str,
    write_header: bool,
    filename_mode: str,
    strict_mode: bool,
    logger,
    log_mode: str,
    batch_rows: int,
) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    # 局部绑定加速
    _sorted = sorted
    _format = _format_filename
    _to = _to_cell
    _load = _load_json_text_compat
    _eval = _eval_compiled
    _logf = _log

    selectors_only = [sel for (sel, _) in compiled_selectors]

    with out_csv.open("w", encoding=encoding, newline="") as f:
        w = csv.writer(f)

        if write_header:
            w.writerow(["file"] + selectors_only)

        buf: List[List[str]] = []
        # 保持原行为：sorted(json_files)
        for jf in _sorted(json_files):
            try:
                data = _load(jf)
                row = [_format(jf, model_root, filename_mode)]
                # 关键路径：用 compiled ops eval（避免每个 selector 反复解析字符串）
                for _, ops in compiled_selectors:
                    v = _eval(data, ops)
                    row.append(_to(v))
                buf.append(row)

                if len(buf) >= batch_rows:
                    w.writerows(buf)
                    buf.clear()
            except Exception as e:
                msg = f"[collect] 处理失败：{jf} err={e}"
                if strict_mode:
                    raise RuntimeError(msg)
                _logf(logger, log_mode, msg)

        if buf:
            w.writerows(buf)


# ----------------------------
# 多文件模式（每 selector 一个 CSV）
# ----------------------------

def _write_multi_csv_for_selector(
    model_root: Path,
    json_files: List[Path],
    selector: str,
    compiled_ops: List[CompiledOp],
    out_csv: Path,
    encoding: str,
    write_header: bool,
    filename_mode: str,
    strict_mode: bool,
    logger,
    log_mode: str,
    batch_rows: int,
) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    _sorted = sorted
    _format = _format_filename
    _to = _to_cell
    _load = _load_json_text_compat
    _eval = _eval_compiled
    _logf = _log

    with out_csv.open("w", encoding=encoding, newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["file", "value"])

        buf: List[List[str]] = []
        for jf in _sorted(json_files):
            try:
                data = _load(jf)
                v = _eval(data, compiled_ops)
                buf.append([_format(jf, model_root, filename_mode), _to(v)])

                if len(buf) >= batch_rows:
                    w.writerows(buf)
                    buf.clear()
            except Exception as e:
                msg = f"[collect] 处理失败：{jf} selector={selector} err={e}"
                if strict_mode:
                    raise RuntimeError(msg)
                _logf(logger, log_mode, msg)

        if buf:
            w.writerows(buf)
