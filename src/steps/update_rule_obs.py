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
6) 必须按 preset.region 使用 global.obsutilconfig_paths[region] 的 -config，避免串配置
7) 为保留 obsutil 进度条/实时输出：不使用 capture_output
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

    # bucket/region from presets
    presets = global_cfg["presets"]
    current_preset: str = str(step_cfg.get("current_preset", "shanghai_dev"))
    if current_preset not in presets:
        raise KeyError(f"[update_rule_obs] preset not found: {current_preset}")

    preset = presets[current_preset] or {}
    bucket: str = str(preset.get("obs_bucket", "")).strip()
    region: str = str(preset.get("region", "")).strip().lower()

    if not bucket:
        raise ValueError(f"[update_rule_obs] preset missing obs_bucket: {current_preset}")
    if not region:
        raise ValueError(f"[update_rule_obs] preset missing region: {current_preset}")

    obsutilconfig_paths: Dict[str, str] = global_cfg.get("obsutilconfig_paths") or {}
    obs_cfg_path = str(obsutilconfig_paths.get(region, "")).strip()
    if not obs_cfg_path:
        raise ValueError(f"[update_rule_obs] missing global.obsutilconfig_paths for region={region} (preset={current_preset})")

    # obsutil settings
    obsutil_exe = step_cfg.get("obsutil_exe") or "obsutil"
    parallel = int(step_cfg.get("parallel", 8))
    jobs = int(step_cfg.get("jobs", 8))
    force = bool(step_cfg.get("force", False))

    enable_full = bool(step_cfg.get("enable_full", False))

    obs_prefix_base_tpl = str(step_cfg.get("obs_prefix_base", "data-collector-svc/range/{model}/base/"))
    obs_prefix_full_tpl = str(step_cfg.get("obs_prefix_full", "data-collector-svc/range/{model}/full/"))

    csv_output_dirname = str(step_cfg.get("csv_output_dirname", "csv_output"))

    time_format = str(step_cfg.get("time_format", "%Y%m%d_%H%M%S"))
    time_tag = datetime.now().strftime(time_format)  # 按你要求：不带时区

    model_to_ranges_csv: Dict[str, Path] = runtime.get("model_to_ranges_csv", {}) or {}
    model_to_ranges_full_csv: Dict[str, Path] = runtime.get("model_to_ranges_full_csv", {}) or {}

    _log(logger, log_mode, f"[update_rule_obs] preset={current_preset} region={region} bucket={bucket} time_tag={time_tag}")
    _log(logger, log_mode, f"[update_rule_obs] using obsutil config: {obs_cfg_path}")

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
            obsutil_config=obs_cfg_path,
            bucket=bucket,
            prefix=base_prefix,
            dry_run=dry_run,
            logger=logger,
            log_mode=log_mode,
        )

        _obsutil_cp_file(
            obsutil_exe=obsutil_exe,
            obsutil_config=obs_cfg_path,
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
        full_prefix: Optional[str] = None
        if enable_full:
            full_local = _resolve_local_csv(
                repo_root=repo_root,
                model=model,
                prefer=model_to_ranges_full_csv.get(model),
                fallback=repo_root / "csv_output" / model / f"{model}_ranges_full.csv",
            )

            full_prefix = _format_prefix(obs_prefix_full_tpl, model=model, time_tag=time_tag)
            full_dst = f"obs://{bucket}/{full_prefix}{time_tag}_range_full.csv"

            _ensure_obs_dir(
                obsutil_exe=obsutil_exe,
                obsutil_config=obs_cfg_path,
                bucket=bucket,
                prefix=full_prefix,
                dry_run=dry_run,
                logger=logger,
                log_mode=log_mode,
            )

            _obsutil_cp_file(
                obsutil_exe=obsutil_exe,
                obsutil_config=obs_cfg_path,
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
        _log(
            logger,
            log_mode,
            f"[update_rule_obs] model={model} uploaded base={base_dst}"
            + (f" full={uploaded_full.get(model)}" if enable_full else ""),
        )

        # 保存上传路径信息到 {model}_last_path.txt
        _save_last_path(
            repo_root=repo_root,
            csv_output_dirname=csv_output_dirname,
            model=model,
            base_prefix=base_prefix.rstrip("/"),
            full_prefix=full_prefix.rstrip("/") if enable_full and full_prefix else None,
            dry_run=dry_run,
            logger=logger,
            log_mode=log_mode,
        )

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


def _ensure_obs_dir(
    obsutil_exe: str,
    obsutil_config: str,
    bucket: str,
    prefix: str,
    dry_run: bool,
    logger,
    log_mode: str,
) -> None:
    folder_url = f"obs://{bucket}/{prefix}".rstrip("/")  # mkdir 通常不需要末尾 /
    cmd = [obsutil_exe, "mkdir", folder_url, f"-config={obsutil_config}"]

    if dry_run:
        _log(logger, log_mode, f"[update_rule_obs] DRY_RUN mkdir: {' '.join(cmd)}")
        return

    _log(logger, log_mode, f"[update_rule_obs] mkdir: {' '.join(cmd)}")
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"[update_rule_obs] obsutil mkdir failed: code={proc.returncode} cmd={' '.join(cmd)}"
        )


def _obsutil_cp_file(
    obsutil_exe: str,
    obsutil_config: str,
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
        f"-config={obsutil_config}",
    ]
    if force:
        cmd.append("-f")

    if dry_run:
        _log(logger, log_mode, f"[update_rule_obs] DRY_RUN cp({tag}): {' '.join(cmd)}")
        return

    _log(logger, log_mode, f"[update_rule_obs] cp({tag}): {' '.join(cmd)}")
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"[update_rule_obs] obsutil cp failed({tag}): code={proc.returncode} cmd={' '.join(cmd)}"
        )


def _save_last_path(
    repo_root: Path,
    csv_output_dirname: str,
    model: str,
    base_prefix: str,
    full_prefix: Optional[str],
    dry_run: bool,
    logger,
    log_mode: str,
) -> None:
    """
    保存上传路径信息到 {model}_last_path.txt
    内容格式（两行）：
      data-collector-svc/range/{model}/base/{time}
      data-collector-svc/range/{model}/full/{time}
    """
    output_dir = repo_root / csv_output_dirname / model
    output_dir.mkdir(parents=True, exist_ok=True)

    last_path_file = output_dir / f"{model}_last_path.txt"

    lines = [base_prefix]
    if full_prefix is not None:
        lines.append(full_prefix)

    content = "\n".join(lines) + "\n"

    if dry_run:
        _log(logger, log_mode, f"[update_rule_obs] DRY_RUN save_last_path: {last_path_file}")
        return

    last_path_file.write_text(content, encoding="utf-8")
    _log(logger, log_mode, f"[update_rule_obs] saved path info to {last_path_file}")


def _log(logger, log_mode: str, msg: str) -> None:
    _ = log_mode
    if logger is None:
        print(msg)
    else:
        logger.info(msg)
