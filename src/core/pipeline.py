# src/core/pipeline.py
# -*- coding: utf-8 -*-
"""
pipeline 调度器
- 固定 step 顺序（不可变）
- 支持 global.steps_to_run 选择性执行（顺序仍保持，只是跳过）
- download 自动发现 models，并注入 runtime
- 若 download 被跳过：固定使用 repo_root/obs_download 作为数据根目录，并自动扫描 models
- 强制错误上报：默认遇错中断；可通过 global.continue_on_error 改为继续
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.step_registry import STEP_ORDER, get_step_callable


def run_pipeline(repo_root: Path, cfg: Dict[str, Any], logger) -> None:
    g = cfg["global"]
    log_mode: str = g.get("log_mode", "normal")
    dry_run: bool = bool(g.get("dry_run", False))
    continue_on_error: bool = bool(g.get("continue_on_error", False))
    steps_to_run: Optional[List[str]] = g.get("steps_to_run")

    steps_to_run_set: Optional[set[str]] = set(steps_to_run) if steps_to_run is not None else None

    # runtime：跨 step 共享的上下文（尽量简单，减少耦合）
    runtime: Dict[str, Any] = {
        "logger": logger,
        "log_mode": log_mode,
        "dry_run": dry_run,
    }

    # ✅ 关键：如果 download 被跳过，则提前把 obs_download_root/models 注入 runtime
    _bootstrap_runtime_if_download_skipped(
        repo_root=repo_root,
        steps_to_run_set=steps_to_run_set,
        runtime=runtime,
        logger=logger,
    )

    logger.info("============================================================")
    logger.info("[START] pipeline log_mode=%s dry_run=%s continue_on_error=%s", log_mode, dry_run, continue_on_error)
    logger.info("============================================================")

    for step_name in STEP_ORDER:
        if steps_to_run_set is not None and step_name not in steps_to_run_set:
            logger.info("[SKIP] step=%s (not in global.steps_to_run)", step_name)
            continue

        step_cfg = cfg.get(step_name, {})
        if not isinstance(step_cfg, dict):
            raise ValueError(f"step config must be dict: {step_name}")

        logger.info("------------------------------------------------------------")
        logger.info("[STEP] %s", step_name)

        fn = get_step_callable(repo_root, step_name)

        try:
            result = fn(
                repo_root=repo_root,
                global_cfg=cfg["global"],
                step_cfg=step_cfg,
                runtime=runtime,
            )
            _merge_step_result_into_runtime(step_name, result, runtime, logger)
            logger.info("[OK] step=%s", step_name)
        except Exception as e:
            logger.exception("[ERROR] step=%s failed: %s", step_name, e)
            if not continue_on_error:
                raise
            logger.info("[WARN] continue_on_error=True -> 继续执行后续 step")

    logger.info("============================================================")
    logger.info("[END] pipeline finished")
    logger.info("============================================================")


def _bootstrap_runtime_if_download_skipped(
    repo_root: Path,
    steps_to_run_set: Optional[set[str]],
    runtime: Dict[str, Any],
    logger,
) -> None:
    """
    当 download 被跳过时：
    - 固定使用 repo_root/obs_download 作为数据根目录
    - 自动扫描 models（obs_download 下的一级子目录）
    这样 selectors/collect/find_range/... 不依赖 download 也能跑。
    """
    if steps_to_run_set is None:
        # 未限制 steps_to_run：说明 download 大概率会执行，不做 bootstrap
        return

    if "download" in steps_to_run_set:
        # download 会执行：由 download step 注入 runtime
        return

    obs_download_root = repo_root / "obs_download"
    if not obs_download_root.exists():
        raise FileNotFoundError(
            f"download 被跳过，但未找到数据根目录：{obs_download_root}\n"
            f"请先执行 download，或手动准备 obs_download 目录。"
        )

    # 注入固定路径
    runtime["obs_download_root"] = obs_download_root

    # 自动发现 models：取一级子目录名（并做简单过滤：跳过看起来像 taskid 的目录）
    models: List[str] = []
    for p in obs_download_root.iterdir():
        if not p.is_dir():
            continue
        name = p.name

        # 软过滤：32位 hex 看起来是 taskid，则不当作 model
        #（避免出现“obs_download/<taskid>/...” 这种未分组状态被误判）
        if _looks_like_taskid(name):
            continue

        models.append(name)

    models = sorted(models)
    runtime["models"] = models

    logger.info("[bootstrap] download skipped -> obs_download_root=%s", obs_download_root)
    logger.info("[bootstrap] discovered models=%d", len(models))


def _looks_like_taskid(s: str) -> bool:
    """
    判断是否像 taskid（32位 hex）
    - 这里不引入 re，保持轻量
    """
    if len(s) != 32:
        return False
    for ch in s:
        if ch not in "0123456789abcdefABCDEF":
            return False
    return True


def _merge_step_result_into_runtime(step_name: str, result: Any, runtime: Dict[str, Any], logger) -> None:
    """
    将各 step 的返回值“转成 runtime 里后续需要的 key”
    这样未来你加 step 时：
    - 新 step 只要返回一个 dataclass 或 dict
    - 在这里补一段 merge 映射
    其它文件基本不用改。
    """
    # 允许 step 返回 None（例如被跳过）
    if result is None:
        return

    # dataclass -> dict
    if hasattr(result, "__dataclass_fields__"):
        d = asdict(result)
    elif isinstance(result, dict):
        d = result
    else:
        # 兜底：直接挂载原始对象
        runtime[f"{step_name}_result"] = result
        return

    if step_name == "download":
        # DownloadResult(models, collect_root, obs_download_root)
        runtime["models"] = d.get("models", [])
        runtime["collect_root"] = Path(d["collect_root"])
        runtime["obs_download_root"] = Path(d["obs_download_root"])
        logger.info("[download] models=%s", runtime["models"])

    elif step_name == "selectors":
        runtime["model_to_selectors_txt"] = {k: Path(v) for k, v in d.get("model_to_selectors_txt", {}).items()}

    elif step_name == "collect":
        runtime["model_to_values_csv"] = {k: Path(v) for k, v in d.get("model_to_values_csv", {}).items()}

    elif step_name == "find_range":
        runtime["model_to_ranges_csv"] = {k: Path(v) for k, v in d.get("model_to_ranges_csv", {}).items()}

    elif step_name == "find_range_full":
        runtime["model_to_ranges_full_csv"] = {k: Path(v) for k, v in d.get("model_to_ranges_full_csv", {}).items()}

    elif step_name == "test_range":
        # 覆盖 ranges.csv 映射（因为 test_range 可能输出到新路径）
        runtime["model_to_ranges_csv"] = {k: Path(v) for k, v in d.get("model_to_ranges_csv", {}).items()}

    elif step_name == "generate_info":
        runtime["model_to_info_csv"] = {k: Path(v) for k, v in d.get("model_to_info_csv", {}).items()}

    elif step_name == "generate_db_csv":
        runtime["model_to_db_dir"] = {k: Path(v) for k, v in d.get("model_to_db_dir", {}).items()}

    elif step_name == "import_db":
        runtime["imported_models"] = list(d.get("imported_models", []))

    elif step_name == "pack_csv":
        runtime["model_to_ranges_txt"] = {k: Path(v) for k, v in d.get("model_to_ranges_txt", {}).items()}

    elif step_name == "build_sql":
        runtime["sql_path"] = Path(d.get("sql_path"))

    elif step_name == "update_rule_obs":
        runtime["uploaded_models_obs"] = list(d.get("uploaded_models", []))
        runtime["uploaded_time_tag"] = str(d.get("time_tag", ""))
        runtime["model_to_uploaded_base"] = dict(d.get("model_to_uploaded_base", {}) or {})
        runtime["model_to_uploaded_full"] = dict(d.get("model_to_uploaded_full", {}) or {})

    elif step_name == "update_rule_api":
        runtime["updated_models"] = list(d.get("updated_models", []))

    else:
        runtime[f"{step_name}_result"] = d
