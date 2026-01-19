# src/steps/pack_csv_txt.py
"""
pack_csv step
职责：
1) 选择 ranges.csv（优先 *_ranges.csv，且可优先表头含“通过率/区间min/区间max”的 ranges 表）
2) 导出为 ranges.txt（literal 单字符串：只显示 \\n，不显示 \\r；并先转义反斜杠，确保可逆）
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class PackCsvResult:
    model_to_ranges_txt: Dict[str, Path]


def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> PackCsvResult:
    logger = runtime.get("logger")
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode", "normal"))

    models: List[str] = runtime["models"]
    model_to_ranges_csv: Dict[str, Path] = runtime.get("model_to_ranges_csv", {}) or {}

    output_suffix: str = str(step_cfg.get("output_suffix", ".txt"))

    out_map: Dict[str, Path] = {}

    for model in models:
        fixed_csv = repo_root / "csv_output" / model / f"{model}_ranges.csv"
        fallback_csv = model_to_ranges_csv.get(model)

        in_csv = fixed_csv if fixed_csv.exists() else (Path(fallback_csv) if fallback_csv else None)

        if not in_csv or not Path(in_csv).exists():
            _log(logger, log_mode, f"[pack_csv] model={model} ranges.csv 不存在 -> 跳过")
            continue

        csv_text = _read_text_keep_newlines(in_csv, encoding="utf-8-sig")
        literal = _escape_for_literal(csv_text)

        out_dir = repo_root / "csv_output" / model
        out_dir.mkdir(parents=True, exist_ok=True)
        suffix = output_suffix if output_suffix.startswith(".") else ("." + output_suffix)
        out_txt = out_dir / f"{model}_ranges{suffix}"

        _write_text_no_newline_translation(out_txt, literal, encoding="utf-8")

        out_map[model] = out_txt
        _log(logger, log_mode, f"[pack_csv] model={model} -> {out_txt} (source={in_csv})")

    return PackCsvResult(model_to_ranges_txt=out_map)


# =============================================================================
# literal 导出（与你给的脚本一致的语义）
# =============================================================================

def _read_text_keep_newlines(path: Path, encoding: str = "utf-8-sig") -> str:
    # Path.read_text 没有 newline 参数，这里必须 open
    with path.open("r", encoding=encoding, newline="") as f:
        return f.read()


def _write_text_no_newline_translation(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=encoding, newline="") as f:
        f.write(text)


def _escape_for_literal(text: str) -> str:
    """
    输出为“单个字符串”，只显示 \\n，不显示 \\r。
    同时先转义反斜杠，避免原文里的 \\n 被和换行混淆，确保可逆。
    """
    text = text.replace("\\", "\\\\")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", "\\n")
    return text


def _log(logger, log_mode: str, msg: str) -> None:
    if logger is None:
        print(msg)
        return
    logger.info(msg)
