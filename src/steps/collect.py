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
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class CollectResult:
    model_to_values_csv: Dict[str, Path]


# =========================
# 可选：更快的 JSON 解析
# =========================
try:
    import orjson  # type: ignore
except Exception:
    orjson = None


def _load_json(path: Path) -> Any:
    if orjson is not None:
        return orjson.loads(path.read_bytes())
    return json.loads(path.read_text(encoding="utf-8"))


# =========================
# selectors.txt 读取
# =========================
def _load_selectors_from_txt(path: Path) -> List[str]:
    if not path.exists():
        raise FileNotFoundError(f"selectors.txt not found: {path}")

    lines = path.read_text(encoding="utf-8").splitlines()
    out: List[str] = []

    for line in lines:
        s = (line or "").strip()
        if not s or s.startswith("#"):
            continue

        # 兼容旧版 wrap_csv=True 的每行末尾逗号
        if s.endswith(","):
            s = s[:-1].rstrip()

        # 去掉包裹引号
        if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
            s = s[1:-1]

        # 兼容 \" 反转义
        s = s.replace('\\"', '"').strip()
        if s:
            out.append(s)

    # 去重保序
    seen = set()
    uniq: List[str] = []
    for x in out:
        if x in seen:
            continue
        seen.add(x)
        uniq.append(x)
    return uniq


# =========================
# selector 预解析
# =========================
@dataclass(frozen=True)
class ParsedSelector:
    raw: str
    segments: List[Tuple[str, Any]]  # ("field", str) / ("index", int) / ("filter", (k, v))


def _parse_selector(selector: str, strict: bool = False) -> ParsedSelector:
    def _fail(msg: str):
        if strict:
            raise ValueError(msg)
        return ParsedSelector(selector, [])

    s = (selector or "").strip()
    if not s:
        return ParsedSelector("", [])

    if not s.startswith("."):
        return _fail("selector must start with '.'")

    def _parse_angle(ss: str, i: int) -> Tuple[str, int]:
        j = ss.find(">", i + 1)
        if j < 0:
            raise ValueError("missing '>'")
        return ss[i + 1 : j], j + 1

    def _parse_paren_index(ss: str, i: int) -> Tuple[int, int]:
        j = ss.find(")", i + 1)
        if j < 0:
            raise ValueError("missing ')'")
        inner = ss[i + 1 : j].strip()
        if not re.fullmatch(r"-?\d+", inner):
            raise ValueError(f"invalid index: {inner}")
        return int(inner), j + 1

    def _parse_bracket_filter(ss: str, i: int) -> Tuple[Tuple[str, Any], int]:
        j = ss.find("]", i + 1)
        if j < 0:
            raise ValueError("missing ']'")
        inner = ss[i + 1 : j].strip()

        if "=" not in inner:
            raise ValueError("filter must be [key=value]")
        key_raw, value_raw = inner.split("=", 1)
        key_raw = key_raw.strip()
        value_raw = value_raw.strip()

        if key_raw.startswith("<") and key_raw.endswith(">"):
            key = key_raw[1:-1]
        else:
            key = key_raw

        if value_raw.startswith("<") and value_raw.endswith(">"):
            value = value_raw[1:-1]
        else:
            vt = value_raw
            if re.fullmatch(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", vt) or vt in ("true", "false", "null"):
                try:
                    value = json.loads(vt)
                except Exception:
                    value = vt
            else:
                value = vt

        return (key, value), j + 1

    segs: List[Tuple[str, Any]] = []
    i = 0
    n = len(s)

    while i < n:
        if s[i] != ".":
            return _fail(f"invalid selector at pos {i}, expected '.'")

        i += 1
        if i >= n:
            return _fail("dangling '.' at end")

        ch = s[i]

        try:
            if ch == "<":
                name, i = _parse_angle(s, i)
                segs.append(("field", name))
                continue
            if ch == "(":
                idx, i = _parse_paren_index(s, i)
                segs.append(("index", idx))
                continue
            if ch == "[":
                (k, v), i = _parse_bracket_filter(s, i)
                segs.append(("filter", (k, v)))
                continue

            # 旧版支持裸字段：.foo.bar
            j = s.find(".", i)
            if j < 0:
                token = s[i:].strip()
                i = n
            else:
                token = s[i:j].strip()
                i = j

            if not token:
                return _fail("empty field name")
            segs.append(("field", token))

        except Exception as e:
            return _fail(f"parse error near pos {i}: {e}")

    return ParsedSelector(raw=s, segments=segs)


def _eval_parsed_selector(data: Any, ps: ParsedSelector, strict: bool = False) -> Any:
    def _fail(msg: str) -> Any:
        if strict:
            raise ValueError(msg)
        return None

    cur: Any = data

    for typ, arg in ps.segments:
        if typ == "field":
            if not isinstance(cur, dict):
                return _fail(f"current value is not object, cannot access field '{arg}'")
            if arg not in cur:
                return _fail(f"field not found: {arg}")
            cur = cur[arg]
            continue

        if typ == "index":
            if not isinstance(cur, list):
                return _fail(f"current value is not array, cannot index ({arg})")
            idx = int(arg)
            if idx < 0 or idx >= len(cur):
                return _fail(f"index out of range: {idx}")
            cur = cur[idx]
            continue

        if typ == "filter":
            if not isinstance(cur, list):
                return _fail(f"current value is not array, cannot filter [{arg[0]}={arg[1]}]")
            key, expect = arg
            found = None
            for item in cur:
                if not isinstance(item, dict):
                    continue
                if key not in item:
                    continue
                # ✅ 旧版：按 JSON 类型相等比较（不是字符串化）
                if item[key] == expect:
                    found = item
                    break
            if found is None:
                return _fail(f"no match for filter [{key}={expect}]")
            cur = found
            continue

        return _fail(f"unknown segment type: {typ}")

    return cur


# =========================
# I/O 工具（与旧版一致语义）
# =========================
def _iter_json_files(root_dir: Path, suffix: str = ".json") -> List[Path]:
    if root_dir.is_file():
        return [root_dir] if root_dir.name.endswith(suffix) else []
    files = [p for p in root_dir.rglob(f"*{suffix}") if p.is_file()]
    files.sort()
    return files


def _to_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return str(v)


def _format_filename(path: Path, model_root: Path, mode: str) -> str:
    if mode == "relative":
        try:
            return str(path.relative_to(model_root)).replace("\\", "/")
        except Exception:
            return str(path)
    return path.name


def _is_empty_value(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip() == ""
    if isinstance(v, (list, dict)):
        return len(v) == 0
    return False


def _safe_selector_filename(selector: str) -> str:
    s = selector
    for ch in '<>:"/\\|?*[]=':
        s = s.replace(ch, "_")
    s = s.replace(".", "_")
    if len(s) > 120:
        s = s[:120]
    return f"{s}.csv"


def _log(logger, msg: str) -> None:
    if logger is None:
        print(msg)
        return
    logger.info(msg)


# =========================
# step 入口
# =========================
def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> CollectResult:
    logger = runtime.get("logger")
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

    # 兼容旧版：可选移除“跨所有文件全空”的字段
    remove_all_empty_fields: bool = bool(step_cfg.get("remove_all_empty_fields", False))

    # 新版保留的性能配置：批量 writerows（不影响解析语义）
    batch_rows: int = int(step_cfg.get("batch_rows", 1024))
    if batch_rows <= 0:
        batch_rows = 1

    model_to_values: Dict[str, Path] = {}

    for model in models:
        model_root = obs_download_root / model
        if not model_root.exists():
            _log(logger, f"[collect] model={model} 不存在目录：{model_root} -> 跳过")
            continue

        selectors_txt = model_to_selectors_txt.get(model)
        if not selectors_txt or not Path(selectors_txt).exists():
            _log(logger, f"[collect] model={model} 缺少 selectors.txt -> 跳过")
            continue

        # selectors 读取
        try:
            selectors_raw = _load_selectors_from_txt(Path(selectors_txt))
        except Exception as e:
            if strict_mode:
                raise
            _log(logger, f"[collect] model={model} 读取 selectors 失败 err={e} -> 跳过")
            continue

        if not selectors_raw:
            _log(logger, f"[collect] model={model} selectors 为空 -> 跳过")
            continue

        json_files = _iter_json_files(model_root, json_suffix)
        if not json_files:
            _log(logger, f"[collect] model={model} 未找到 json -> 跳过")
            continue

        out_dir = csv_output_dir / model
        out_dir.mkdir(parents=True, exist_ok=True)

        # selector 预解析
        parsed_all = [_parse_selector(s, strict=strict_mode) for s in selectors_raw]

        # 模式：单文件输出
        if single_file_mode:
            out_csv = out_dir / f"{model}.csv"

            parsed = parsed_all
            kept_raw = [ps.raw for ps in parsed]

            # 可选：移除跨所有文件全空的字段
            if remove_all_empty_fields:
                keep = [False] * len(parsed_all)
                for jf in json_files:
                    data = _load_json(jf)
                    for i, ps in enumerate(parsed_all):
                        if keep[i]:
                            continue
                        v = _eval_parsed_selector(data, ps, strict=strict_mode)
                        if not _is_empty_value(v):
                            keep[i] = True
                parsed = [ps for ps, k in zip(parsed_all, keep) if k]
                kept_raw = [ps.raw for ps in parsed]

            with out_csv.open("w", encoding=csv_encoding, newline="") as f:
                w = csv.writer(f)
                if csv_write_header:
                    w.writerow(["file"] + kept_raw)

                buf: List[List[str]] = []
                for jf in json_files:
                    try:
                        data = _load_json(jf)
                        row = [_format_filename(jf, model_root, filename_in_csv)]
                        for ps in parsed:
                            v = _eval_parsed_selector(data, ps, strict=strict_mode)
                            row.append(_to_cell(v))
                        buf.append(row)

                        if len(buf) >= batch_rows:
                            w.writerows(buf)
                            buf.clear()
                    except Exception as e:
                        msg = f"[collect] 处理失败：{jf} err={e}"
                        if strict_mode:
                            raise RuntimeError(msg)
                        _log(logger, msg)

                if buf:
                    w.writerows(buf)

            model_to_values[model] = out_csv
            _log(logger, f"[collect] model={model} 完成（single）json_files={len(json_files)} -> {out_csv}")
            continue

        # 模式：多文件输出（每 selector 一个 CSV）
        for i, ps in enumerate(parsed_all, start=1):
            out_csv = out_dir / _safe_selector_filename(ps.raw)

            with out_csv.open("w", encoding=csv_encoding, newline="") as f:
                w = csv.writer(f)
                if csv_write_header:
                    w.writerow(["file", "value"])

                buf: List[List[str]] = []
                for jf in json_files:
                    try:
                        data = _load_json(jf)
                        v = _eval_parsed_selector(data, ps, strict=strict_mode)
                        if remove_all_empty_fields and _is_empty_value(v):
                            continue
                        buf.append([_format_filename(jf, model_root, filename_in_csv), _to_cell(v)])

                        if len(buf) >= batch_rows:
                            w.writerows(buf)
                            buf.clear()
                    except Exception as e:
                        msg = f"[collect] 处理失败：{jf} selector={ps.raw} err={e}"
                        if strict_mode:
                            raise RuntimeError(msg)
                        _log(logger, msg)

                if buf:
                    w.writerows(buf)

        model_to_values[model] = out_dir / f"{model}.csv"
        _log(logger, f"[collect] model={model} 完成（multi）json_files={len(json_files)}")

    return CollectResult(model_to_values_csv=model_to_values)
