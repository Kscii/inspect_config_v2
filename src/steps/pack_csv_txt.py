# src/steps/pack_csv_txt.py
"""
pack_csv step
职责：
1) 选择 ranges.csv（优先固定命名文件）
2) 导出为 ranges.txt（literal 单字符串：只显示 \\n，不显示 \\r；并先转义反斜杠，确保可逆）
3) 支持 base 与 full 两套各导出一次
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List


@dataclass
class PackCsvResult:
    model_to_ranges_txt: Dict[str, Path]
    model_to_ranges_full_txt: Dict[str, Path]


def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> PackCsvResult:
    logger = runtime.get("logger")
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode", "normal"))

    enable_full: bool = bool(step_cfg.get("enable_full", True))

    models: List[str] = runtime["models"]
    model_to_ranges_csv: Dict[str, Path] = runtime.get("model_to_ranges_csv", {}) or {}
    model_to_ranges_full_csv: Dict[str, Path] = runtime.get("model_to_ranges_full_csv", {}) or {}

    output_suffix: str = str(step_cfg.get("output_suffix", ".txt"))
    output_suffix_full: str = str(step_cfg.get("output_suffix_full", "_full.txt"))

    out_base: Dict[str, Path] = {}
    out_full: Dict[str, Path] = {}

    # base
    out_base = _pack_one(
        variant="base",
        repo_root=repo_root,
        logger=logger,
        log_mode=log_mode,
        models=models,
        model_to_ranges_csv=model_to_ranges_csv,
        fixed_csv_name_tpl="{model}_ranges.csv",
        output_suffix=output_suffix,
    )

    # full
    if enable_full:
        out_full = _pack_one(
            variant="full",
            repo_root=repo_root,
            logger=logger,
            log_mode=log_mode,
            models=models,
            model_to_ranges_csv=model_to_ranges_full_csv,
            fixed_csv_name_tpl="{model}_ranges_full.csv",
            output_suffix=output_suffix_full,
        )

    return PackCsvResult(model_to_ranges_txt=out_base, model_to_ranges_full_txt=out_full)


def _pack_one(
    variant: str,
    repo_root: Path,
    logger,
    log_mode: str,
    models: List[str],
    model_to_ranges_csv: Dict[str, Path],
    fixed_csv_name_tpl: str,
    output_suffix: str,
) -> Dict[str, Path]:
    out_map: Dict[str, Path] = {}

    for model in models:
        fixed_csv = repo_root / "csv_output" / model / fixed_csv_name_tpl.format(model=model)
        fallback_csv = model_to_ranges_csv.get(model)

        in_csv = fixed_csv if fixed_csv.exists() else (Path(fallback_csv) if fallback_csv else None)

        if not in_csv or not Path(in_csv).exists():
            _log(logger, log_mode, f"[pack_csv:{variant}] model={model} ranges.csv 不存在 -> 跳过")
            continue

        csv_text = _read_text_keep_newlines(in_csv, encoding="utf-8-sig")
        literal = _escape_for_literal(csv_text)

        out_dir = repo_root / "csv_output" / model
        out_dir.mkdir(parents=True, exist_ok=True)

        suffix = output_suffix
        # 允许 ".txt" 或 "_full.txt" 等形式
        if not (suffix.startswith(".") or suffix.startswith("_")):
            suffix = "." + suffix

        # 规则：输出文件名固定为 {model}_ranges{suffix}
        out_txt = out_dir / f"{model}_ranges{suffix}"

        _write_text_no_newline_translation(out_txt, literal, encoding="utf-8")

        out_map[model] = out_txt

        _log(logger, log_mode, f"[pack_csv:{variant}] model={model} -> {out_txt} (source={in_csv})")

    return out_map


# =============================================================================
# literal 导出
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
