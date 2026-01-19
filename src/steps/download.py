# src/steps/download.py
# -*- coding: utf-8 -*-
"""
download step
职责：
1) 从 OBS 下载数据到“当前路径（repo_root）”（即 obsutil cp obs://{bucket}/{prefix} -> repo_root）
2) 下载完成后，将本地产物中名为 download_dirname（默认 collect）的目录改名为 obs_download_rootname（默认 obs_download）
3) 在本地将任务按“构型(model)”自动发现并分组到 repo_root/obs_download/<model>/<taskid>/...
4) 支持 skip_download / full_refresh / dry_run / 重试 / taskid 过滤等
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class DownloadResult:
    """download 产出：自动发现的构型列表（后续 steps 依赖）"""
    models: List[str]
    collect_root: Path
    obs_download_root: Path


# 先正则快速找 model，命中就不必 json.loads（对大 json 很省）
_MODEL_RE = re.compile(r'"model"\s*:\s*"([^"]+)"', re.IGNORECASE)
_ROBOT_MODEL_RE = re.compile(r'"robotModel"\s*:\s*"([^"]+)"', re.IGNORECASE)


def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> DownloadResult:
    """
    入口函数：runner 会调用这个函数。
    - repo_root: 仓库根目录（也就是你说的“当前路径”）
    - global_cfg: config.yaml 的 global 段（runner 会注入 presets）
    - step_cfg: config.yaml 的 download 段
    - runtime: runner 注入的运行时信息（logger/log_mode/dry_run 等）
    """
    logger = runtime.get("logger")
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode"))
    global_dry_run = bool(runtime.get("dry_run", global_cfg.get("dry_run")))
    dry_run = bool(step_cfg.get("dry_run")) or global_dry_run

    # -------------------------
    # 读取 presets / bucket
    # -------------------------
    presets = global_cfg["presets"]
    current_preset = step_cfg["current_preset"]
    bucket = presets[current_preset]["obs_bucket"]

    obs_prefix = step_cfg["obs_prefix"]
    obs_prefix_norm = obs_prefix.lstrip("/")  # 仅用于日志/兼容查找

    # -------------------------
    # 本地目录命名（按配置）
    # -------------------------
    download_dirname = str(step_cfg.get("download_dirname"))  # 通常是 collect
    obs_download_rootname = str(step_cfg.get("obs_download_rootname"))

    collect_root = repo_root / download_dirname
    obs_download_root = repo_root / obs_download_rootname

    # -------------------------
    # 下载/搬运相关配置
    # -------------------------
    parallel = int(step_cfg.get("parallel"))
    jobs = int(step_cfg.get("jobs"))
    force = bool(step_cfg.get("force"))
    obsutil_exe = step_cfg.get("obsutil_exe")  # None or str

    skip_download = bool(step_cfg.get("skip_download"))
    full_refresh = bool(step_cfg.get("full_refresh"))

    default_model = str(step_cfg.get("default_model_if_missing"))
    move_retries = int(step_cfg.get("move_retries"))
    filter_taskid_dirs = bool(step_cfg.get("filter_taskid_dirs"))

    illegal_win_pattern = step_cfg.get("illegal_win_pattern")
    taskid_regex = step_cfg.get("taskid_regex")
    illegal_win_re = re.compile(illegal_win_pattern)
    taskid_re = re.compile(taskid_regex, re.IGNORECASE)

    # -------------------------
    # 0) full_refresh：清理旧目录
    # -------------------------
    if full_refresh:
        _log(logger, log_mode, f"[download] full_refresh=True -> 清理 {collect_root} 和 {obs_download_root}")
        if not dry_run:
            shutil.rmtree(collect_root, ignore_errors=True)
            shutil.rmtree(obs_download_root, ignore_errors=True)

    # -------------------------
    # 1) 下载（可选）
    # -------------------------
    if not skip_download:
        _log(logger, log_mode, f"[download] 开始从 OBS 下载到当前路径：bucket={bucket}, prefix={obs_prefix}, dst={repo_root}")
        _obsutil_cp_prefix_to_repo_root(
            obsutil_exe=obsutil_exe,
            bucket=bucket,
            obs_prefix=obs_prefix,
            repo_root=repo_root,
            parallel=parallel,
            jobs=jobs,
            force=force,
            dry_run=dry_run,
            logger=logger,
        )
    else:
        _log(logger, log_mode, "[download] skip_download=True -> 跳过下载，仅做改名与分组（要求本地已有下载产物）")

    # -------------------------
    # 2) 将下载出来的 “collect” 改名为 “obs_download”
    # -------------------------
    downloaded_root = _find_downloaded_collect_root(
        repo_root=repo_root,
        expect_collect_root=collect_root,
        obs_prefix_norm=obs_prefix_norm,
        logger=logger,
        log_mode=log_mode,
    )
    if downloaded_root is None:
        raise FileNotFoundError(
            f"未找到下载产物目录：期望 {collect_root} 或可从 prefix 推断的目录。"
            f"repo_root={repo_root}, obs_prefix={obs_prefix}"
        )

    if downloaded_root.resolve() != collect_root.resolve():
        _log(logger, log_mode, f"[download] 下载产物目录与期望不一致：actual={downloaded_root} expect={collect_root} -> 先统一到 expect")
        if not dry_run:
            if collect_root.exists():
                shutil.rmtree(collect_root, ignore_errors=True)
            shutil.move(str(downloaded_root), str(collect_root))

    if not collect_root.exists():
        raise FileNotFoundError(f"collect_root not found after download/normalize: {collect_root}")

    _log(logger, log_mode, f"[download] 改名：{collect_root.name} -> {obs_download_root.name}")
    if not dry_run:
        if obs_download_root.exists():
            shutil.rmtree(obs_download_root, ignore_errors=True)
        shutil.move(str(collect_root), str(obs_download_root))

    working_root = obs_download_root if not dry_run else collect_root

    # -------------------------
    # 3) 自动发现 taskid 目录，并按 model 分组到 obs_download/<model>/<taskid>/...
    # -------------------------
    if not working_root.exists():
        raise FileNotFoundError(f"working_root not found: {working_root}")

    # 更快的扫描：os.scandir（避免 Path.iterdir 产生大量对象开销）
    task_dirs: List[Path] = []
    with os.scandir(working_root) as it:
        for e in it:
            if e.is_dir():
                task_dirs.append(Path(e.path))

    if filter_taskid_dirs:
        task_dirs = [p for p in task_dirs if taskid_re.match(p.name or "")]
    else:
        # 软过滤：优先只取像 taskid 的目录；如果一个都匹配不到则全放行
        filtered = [p for p in task_dirs if taskid_re.match(p.name or "")]
        task_dirs = filtered or task_dirs

    _log(logger, log_mode, f"[download] 扫描 task 目录数：{len(task_dirs)} (filter_taskid_dirs={filter_taskid_dirs})")

    model_set: set[str] = set()

    # 不强依赖顺序时不要 sorted（避免额外 O(n log n) + 字符串比较）
    # 如果你确实需要稳定顺序，可以把下面这一行取消注释：
    # task_dirs = sorted(task_dirs, key=lambda x: x.name)

    for task_dir in task_dirs:
        if not task_dir.is_dir():
            continue

        # 只需要判断“有没有 episode 子目录”，不做全量 list/sorted
        if not _has_any_subdir(task_dir):
            continue

        sample_json = _pick_sample_json_fast(task_dir)
        model = _detect_model_from_collect_json_fast(
            sample_json,
            default_model=default_model,
            logger=logger,
            log_mode=log_mode,
        )
        model = _sanitize_name(model, illegal_win_re=illegal_win_re, fallback=default_model)
        model_set.add(model)

        dst_task_dir = working_root / model / task_dir.name
        _log(logger, log_mode, f"[download] task={task_dir.name} -> model={model} -> {dst_task_dir}")

        if dry_run:
            continue

        # 关键优化：优先 O(1) rename/move；仅当 dst 已存在才 fallback 合并 copytree
        _move_task_dir_fast(
            src=task_dir,
            dst=dst_task_dir,
            retries=move_retries,
            logger=logger,
            log_mode=log_mode,
        )

    models = sorted(model_set)
    _log(logger, log_mode, f"[download] 自动发现构型：{models}")

    return DownloadResult(models=models, collect_root=working_root, obs_download_root=working_root)


# =============================================================================
# 内部工具函数
# =============================================================================

def _log(logger, log_mode: str, msg: str) -> None:
    if logger is None:
        print(msg)
        return
    logger.info(msg)


def _obsutil_cp_prefix_to_repo_root(
    obsutil_exe: Optional[str],
    bucket: str,
    obs_prefix: str,
    repo_root: Path,
    parallel: int,
    jobs: int,
    force: bool,
    dry_run: bool,
    logger,
) -> None:
    """使用 obsutil 将 obs://bucket/obs_prefix 整体拉到 repo_root（当前路径）"""
    exe = obsutil_exe or "obsutil"
    src = f"obs://{bucket}/{obs_prefix.lstrip('/')}"
    dst = str(repo_root)

    cmd = [
        exe,
        "cp",
        src,
        dst,
        "-r",
        "-p",
        str(parallel),
        "-j",
        str(jobs),
    ]
    if force:
        cmd.append("-f")

    if dry_run:
        if logger:
            logger.info("[download] DRY_RUN obsutil cmd: %s", " ".join(cmd))
        else:
            print("[download] DRY_RUN obsutil cmd:", " ".join(cmd))
        return

    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"obsutil cp failed, returncode={proc.returncode}")


def _find_downloaded_collect_root(
    repo_root: Path,
    expect_collect_root: Path,
    obs_prefix_norm: str,
    logger,
    log_mode: str,
) -> Optional[Path]:
    # 目前仍保持你原来的最严格行为：只认 repo_root/collect
    if expect_collect_root.exists() and expect_collect_root.is_dir():
        return expect_collect_root
    return None


def _has_any_subdir(d: Path) -> bool:
    """比 list(itertools) 更省：只要找到一个子目录就返回 True"""
    try:
        with os.scandir(d) as it:
            for e in it:
                if e.is_dir():
                    return True
    except FileNotFoundError:
        return False
    return False


def _pick_sample_json_fast(task_dir: Path) -> Optional[Path]:
    """
    更快版本：
    - 找到任意一个 episode 子目录就用（不排序）
    - 优先返回第一个 *_collect.json（不排序）
    - 否则返回第一个 *.json（不排序）
    """
    first_ep: Optional[Path] = None
    try:
        with os.scandir(task_dir) as it:
            for e in it:
                if e.is_dir():
                    first_ep = Path(e.path)
                    break
    except FileNotFoundError:
        return None

    if first_ep is None:
        return None

    # 优先 *_collect.json
    try:
        for p in first_ep.glob("*_collect.json"):
            return p
        for p in first_ep.glob("*.json"):
            return p
    except FileNotFoundError:
        return None
    return None


def _detect_model_from_collect_json_fast(sample_json: Optional[Path], default_model: str, logger, log_mode: str) -> str:
    """
    识别构型（model）的启发式逻辑（性能优化版）：
    1) 先 regex 从文本里抓 "model":"xxx" / "robotModel":"xxx"
    2) 再 json.loads + 轻量 key 查找
    3) 兜底 default_model
    """
    if sample_json is None or not sample_json.exists():
        return default_model

    try:
        text = sample_json.read_text(encoding="utf-8-sig", errors="ignore")
    except Exception as e:
        _log(logger, log_mode, f"[download] 读取样本 json 失败：{sample_json} err={e}")
        return default_model

    m = _MODEL_RE.search(text)
    if m and m.group(1).strip():
        return m.group(1).strip()

    m2 = _ROBOT_MODEL_RE.search(text)
    if m2 and m2.group(1).strip():
        return m2.group(1).strip()

    try:
        data = json.loads(text)
    except Exception as e:
        _log(logger, log_mode, f"[download] 解析样本 json 失败：{sample_json} err={e}")
        return default_model

    v = _get_by_path(data, ("model",))
    if isinstance(v, str) and v.strip():
        return v.strip()

    v2 = _deep_find_first_key(data, keys={"model", "robotModel"})
    if isinstance(v2, str) and v2.strip():
        return v2.strip()

    return default_model


def _get_by_path(obj: Any, path: Tuple[str, ...]) -> Any:
    cur = obj
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _deep_find_first_key(obj: Any, keys: set[str]) -> Any:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys:
                return v
            found = _deep_find_first_key(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for it in obj:
            found = _deep_find_first_key(it, keys)
            if found is not None:
                return found
    return None


def _sanitize_name(name: str, illegal_win_re: re.Pattern, fallback: str) -> str:
    """清洗文件夹名，保证 Windows/跨平台兼容。"""
    s = (name or "").strip()
    if not s:
        return fallback
    s = illegal_win_re.sub("_", s)
    s = s.strip(" .")
    return s or fallback


def _move_task_dir_fast(src: Path, dst: Path, retries: int, logger, log_mode: str) -> None:
    """
    性能关键：
    - dst 不存在：用 os.replace 直接 rename（同盘 O(1)）
    - dst 已存在：才 fallback 到 copytree 合并（慢，但极少触发）
    """
    dst.parent.mkdir(parents=True, exist_ok=True)

    # 常见路径：目标不存在 -> O(1) rename
    if not dst.exists():
        last_err: Optional[Exception] = None
        for i in range(max(1, retries)):
            try:
                os.replace(str(src), str(dst))
                return
            except PermissionError as e:
                last_err = e
                _log(logger, log_mode, f"[download] move PermissionError 重试 {i+1}/{retries} src={src} dst={dst}")
                time.sleep(0.5 * (i + 1))
            except OSError as e:
                # 例如跨盘、文件系统不支持 rename 等
                last_err = e
                break

        _log(logger, log_mode, f"[download] move rename 失败，fallback copytree: src={src} dst={dst} last_err={last_err}")

    # 兜底：合并 copy（慢）
    _copytree_with_retries(
        src=src,
        dst=dst,
        retries=retries,
        logger=logger,
        log_mode=log_mode,
    )
    shutil.rmtree(src, ignore_errors=True)


def _copytree_with_retries(src: Path, dst: Path, retries: int, logger, log_mode: str) -> None:
    """复制目录（支持 dirs_exist_ok=True 合并），对 Windows PermissionError 做重试。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    last_err: Optional[Exception] = None
    for i in range(max(1, retries)):
        try:
            shutil.copytree(src, dst, dirs_exist_ok=True)
            return
        except PermissionError as e:
            last_err = e
            _log(logger, log_mode, f"[download] copytree PermissionError 重试 {i+1}/{retries} src={src} dst={dst}")
            time.sleep(0.5 * (i + 1))
        except Exception as e:
            last_err = e
            break
    raise RuntimeError(f"copytree failed after retries={retries}, last_err={last_err}")
