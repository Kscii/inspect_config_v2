# src/steps/update_rule_obs.py
# -*- coding: utf-8 -*-
"""
update_rule_obs step
职责：
1) 将 ranges.csv 上传到 OBS 指定路径（按时间戳命名，不覆盖）
2) base: <model>_ranges.csv  -> {time}_range_base.csv
3) full: <model>_ranges_full.csv -> {time}_range_full.csv（可选 enable_full）
4) 上传失败：报错并终止
5) 若 OBS 路径不存在：自动创建（obsutil mkdir）
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class UpdateRuleObsResult:
    uploaded_models: List[str]
    time_tag: str
    model_to_uploaded_base: Dict[str, str]
    model_to_uploaded_full: Dict[str, str]


def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> UpdateRuleObsResult:
    logger = runtime.get("logger")
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode", "normal"))
    global_dry_run = bool(runtime.get("dry_run", global_cfg.get("dry_run", False)))
    dry_run = bool(step_cfg.get("dry_run", False)) or global_dry_run

    models: List[str] = runtime["models"]

    # bucket from presets
    presets = global_cfg["presets"]
    current_preset: str = str(step_cfg.get("current_preset", "dev"))
    bucket: str = str(presets[current_preset]["obs_bucket"])

    # obsutil settings
    obsutil_exe = step_cfg.get("obsutil_exe") or "obsutil"
    parallel = int(step_cfg.get("parallel", 8))
    jobs = int(step_cfg.get("jobs", 8))
    force = bool(step_cfg.get("force", False))

    enable_full = bool(step_cfg.get("enable_full", False))

    obs_prefix_base_tpl = str(step_cfg.get("obs_prefix_base", "data-collector-svc/range/{model}/base/"))
    obs_prefix_full_tpl = str(step_cfg.get("obs_prefix_full", "data-collector-svc/range/{model}/full/"))

    time_format = str(step_cfg.get("time_format", "%Y%m%d_%H%M%S"))
    time_tag = datetime.now().strftime(time_format)  # 按你要求：不带时区

    model_to_ranges_csv: Dict[str, Path] = runtime.get("model_to_ranges_csv", {}) or {}
    model_to_ranges_full_csv: Dict[str, Path] = runtime.get("model_to_ranges_full_csv", {}) or {}

    uploaded: List[str] = []
    uploaded_base: Dict[str, str] = {}
    uploaded_full: Dict[str, str] = {}

    for model in models:
        # -------------------------
        # base local file
        # -------------------------
        base_local = _resolve_local_csv(
            repo_root=repo_root,
            model=model,
            prefer=model_to_ranges_csv.get(model),
            fallback=repo_root / "csv_output" / model / f"{model}_ranges.csv",
        )

        base_prefix = _format_prefix(obs_prefix_base_tpl, model=model, time_tag=time_tag)
        base_dst = f"obs://{bucket}/{base_prefix}{time_tag}_range_base.csv"

        _ensure_obs_dir(
            obsutil_exe=obsutil_exe,
            bucket=bucket,
            prefix=base_prefix,
            dry_run=dry_run,
            logger=logger,
            log_mode=log_mode,
        )

        _obsutil_cp_file(
            obsutil_exe=obsutil_exe,
            src=base_local,
            dst=base_dst,
            parallel=parallel,
            jobs=jobs,
            force=force,
            dry_run=dry_run,
            logger=logger,
            log_mode=log_mode,
            tag=f"base:{model}",
        )

        uploaded_base[model] = base_dst

        # -------------------------
        # full local file (optional)
        # -------------------------
        if enable_full:
            full_local = _resolve_local_csv(
                repo_root=repo_root,
                model=model,
                prefer=model_to_ranges_full_csv.get(model),
                fallback=repo_root / "csv_output" / model / f"{model}_ranges_full.csv",
            )

            full_prefix = _format_prefix(obs_prefix_full_tpl, model=model, time_tag=time_tag)
            full_dst = f"obs://{bucket}/{full_prefix}range_full.csv"

            _ensure_obs_dir(
                obsutil_exe=obsutil_exe,
                bucket=bucket,
                prefix=full_prefix,
                dry_run=dry_run,
                logger=logger,
                log_mode=log_mode,
            )

            _obsutil_cp_file(
                obsutil_exe=obsutil_exe,
                src=full_local,
                dst=full_dst,
                parallel=parallel,
                jobs=jobs,
                force=force,
                dry_run=dry_run,
                logger=logger,
                log_mode=log_mode,
                tag=f"full:{model}",
            )
            uploaded_full[model] = full_dst

        uploaded.append(model)
        _log(logger, log_mode, f"[update_rule_obs] model={model} uploaded base={base_dst}" + (f" full={uploaded_full.get(model)}" if enable_full else ""))

    return UpdateRuleObsResult(
        uploaded_models=uploaded,
        time_tag=time_tag,
        model_to_uploaded_base=uploaded_base,
        model_to_uploaded_full=uploaded_full,
    )


# -------------------------
# helpers
# -------------------------

def _format_prefix(tpl: str, model: str, time_tag: str = "") -> str:
    s = (tpl or "").replace("{model}", model).replace("{time}", time_tag)
    s = s.lstrip("/")  # obs://bucket/xxx 不要双斜杠
    return s if s.endswith("/") else (s + "/")


def _resolve_local_csv(repo_root: Path, model: str, prefer: Optional[Path], fallback: Path) -> Path:
    if prefer is not None:
        p = Path(prefer)
        if p.exists():
            return p
    if fallback.exists():
        return fallback
    raise FileNotFoundError(f"[update_rule_obs] model={model} local csv not found: prefer={prefer} fallback={fallback}")


def _ensure_obs_dir(obsutil_exe: str, bucket: str, prefix: str, dry_run: bool, logger, log_mode: str) -> None:
    # obsutil mkdir obs://bucket/folder[/subfolder...]  —— 官方文档支持多级目录创建（重复创建不报错）
    folder_url = f"obs://{bucket}/{prefix}".rstrip("/")  # mkdir 通常不需要末尾 /
    cmd = [obsutil_exe, "mkdir", folder_url]

    if dry_run:
        _log(logger, log_mode, f"[update_rule_obs] DRY_RUN mkdir: {' '.join(cmd)}")
        return

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"[update_rule_obs] obsutil mkdir failed: code={proc.returncode} cmd={' '.join(cmd)} "
            f"stdout={_one_line(proc.stdout)} stderr={_one_line(proc.stderr)}"
        )


def _obsutil_cp_file(
    obsutil_exe: str,
    src: Path,
    dst: str,
    parallel: int,
    jobs: int,
    force: bool,
    dry_run: bool,
    logger,
    log_mode: str,
    tag: str,
) -> None:
    cmd = [
        obsutil_exe,
        "cp",
        str(src),
        dst,
        "-p",
        str(parallel),
        "-j",
        str(jobs),
    ]
    if force:
        cmd.append("-f")

    if dry_run:
        _log(logger, log_mode, f"[update_rule_obs] DRY_RUN cp({tag}): {' '.join(cmd)}")
        return

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"[update_rule_obs] obsutil cp failed({tag}): code={proc.returncode} cmd={' '.join(cmd)} "
            f"stdout={_one_line(proc.stdout)} stderr={_one_line(proc.stderr)}"
        )


def _one_line(s: Optional[str]) -> str:
    if not s:
        return ""
    return " ".join(str(s).replace("\r", " ").replace("\n", " ").split()).strip()


def _log(logger, log_mode: str, msg: str) -> None:
    if logger is None:
        print(msg)
    else:
        logger.info(msg)
