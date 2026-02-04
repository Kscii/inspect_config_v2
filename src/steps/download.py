# src/steps/download.py
# -*- coding: utf-8 -*-
"""
download step
职责：
1) 从 OBS 下载数据到 repo_root（obsutil cp obs://{bucket}/{prefix} -> repo_root）
2) 不再做“collect -> obs_download”的整目录改名/替换
3) 直接把 taskid 目录移动到：repo_root/obs_download/<model>/<taskid>/...
4) 支持 skip_download / full_refresh / dry_run / 重试 / taskid 过滤等
5) 支持 CSV 多前缀下载模式（可选）

"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import stat
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


_MODEL_RE = re.compile(r'"model"\s*:\s*"([^"]+)"', re.IGNORECASE)
_ROBOT_MODEL_RE = re.compile(r'"robotModel"\s*:\s*"([^"]+)"', re.IGNORECASE)


# staging 根目录名（prod_all 用）
_STAGE_DIRNAME = "_obs_stage"


def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> DownloadResult:
    logger = runtime.get("logger")
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode"))
    global_dry_run = bool(runtime.get("dry_run", global_cfg.get("dry_run")))
    dry_run = bool(step_cfg.get("dry_run")) or global_dry_run

    # -------------------------
    # 读取 presets / preset 选择
    # -------------------------
    presets: Dict[str, Any] = global_cfg["presets"]
    current_preset = str(step_cfg["current_preset"])

    # prod_all：按顺序逐个 preset 下载
    # all：从 global.presets 的所有 preset 下载
    preset_list: List[str]
    if current_preset == "prod_all":
        preset_list = list(step_cfg.get("prod_all_presets") or [])
        if not preset_list:
            raise ValueError("[download] current_preset=prod_all but download.prod_all_presets is empty")
    elif current_preset == "all":
        all_presets = step_cfg.get("all_presets") or []
        if all_presets:
            preset_list = list(all_presets)
        else:
            # 自动使用 global.presets 的所有 key
            preset_list = list(presets.keys())
        if not preset_list:
            raise ValueError("[download] current_preset=all but no presets found in global.presets")
    else:
        preset_list = [current_preset]

    # obsutil config 路径映射（按 region 取）
    obsutilconfig_paths: Dict[str, str] = global_cfg.get("obsutilconfig_paths") or {}

    obs_prefix = str(step_cfg["obs_prefix"])

    # -------------------------
    # 本地目录命名
    # -------------------------
    obs_download_rootname = str(step_cfg.get("obs_download_rootname"))
    obs_download_root = repo_root / obs_download_rootname

    # staging 根目录
    stage_root = repo_root / _STAGE_DIRNAME

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
    illegal_win_re = re.compile(str(illegal_win_pattern))
    taskid_re = re.compile(str(taskid_regex), re.IGNORECASE)

    # -------------------------
    # CSV 模式配置（可选）
    # -------------------------
    csv_mode = bool(step_cfg.get("csv_mode", False))
    csv_path = step_cfg.get("csv_path", "taskids.csv")
    csv_col_name = step_cfg.get("csv_col_name", "taskid")
    csv_col_index = int(step_cfg.get("csv_col_index", 0))
    csv_skip_header = bool(step_cfg.get("csv_skip_header", False))
    csv_rows_are_taskid = bool(step_cfg.get("csv_rows_are_taskid", True))

    # -------------------------
    # collect_root 规则（简化）
    # -------------------------
    collect_root = repo_root / "collect"

    _log(logger, log_mode, f"[download] resolved obs_download_root={obs_download_root}")
    _log(logger, log_mode, f"[download] resolved collect_root={collect_root} (csv_mode={csv_mode})")
    _log(logger, log_mode, f"[download] obs_prefix={obs_prefix}")
    _log(logger, log_mode, f"[download] current_preset={current_preset} preset_list={preset_list}")

    # -------------------------
    # 0) full_refresh：只在一开始清理
    # -------------------------
    if full_refresh:
        # 注意：
        # - CSV 模式下可能落盘到多个 leaf 目录，这里仍只清理最终产物与 collect
        # - prod_all 会用 staging，为避免历史残留干扰，也清理 staging
        _log(logger, log_mode, f"[download] full_refresh=True -> 清理 {collect_root}、{obs_download_root}、{stage_root}")
        if not dry_run:
            _rmtree_force(collect_root)
            _rmtree_force(obs_download_root)
            _rmtree_force(stage_root)

    # -------------------------
    # 1) 计算要下载的 prefixes
    # -------------------------
    prefixes_to_download: List[str] = []
    if csv_mode:
        base_prefix = obs_prefix
        rows = _read_csv_rows(
            repo_root=repo_root,
            csv_path=csv_path,
            col_name=csv_col_name,
            col_index=csv_col_index,
            skip_header=csv_skip_header,
            logger=logger,
            log_mode=log_mode,
        )
        if csv_rows_are_taskid:
            base = _ensure_trailing_slash(base_prefix.lstrip("/"))
            for r in rows:
                rid = (r or "").strip().strip("/")
                if not rid:
                    continue
                prefixes_to_download.append(base + rid + "/")
        else:
            for r in rows:
                rr = (r or "").strip()
                if rr:
                    prefixes_to_download.append(rr)
    else:
        prefixes_to_download = [obs_prefix]

    # -------------------------
    # 2) 下载（可选）
    # -------------------------
    # 单 preset：保持原行为，dst=repo_root
    # prod_all ：dst=repo_root/_obs_stage/<preset_name>，避免多个 bucket 混在 repo_root 互相覆盖
    download_jobs: List[Tuple[str, str, str, Path]] = []
    for preset_name in preset_list:
        if preset_name not in presets:
            raise KeyError(f"[download] preset not found in global.presets: {preset_name}")

        preset = presets[preset_name] or {}
        bucket = preset.get("obs_bucket")
        region = preset.get("region")
        if not bucket:
            raise ValueError(f"[download] preset missing obs_bucket: {preset_name}")
        if not region:
            raise ValueError(f"[download] preset missing region: {preset_name}")

        obs_cfg_path = obsutilconfig_paths.get(region)
        if not obs_cfg_path:
            raise ValueError(f"[download] missing global.obsutilconfig_paths for region={region} (preset={preset_name})")

        # 目标目录
        if current_preset in ("prod_all", "all"):
            dst_root = stage_root / preset_name
        else:
            dst_root = repo_root

        download_jobs.append((preset_name, bucket, obs_cfg_path, dst_root))

    if not skip_download:
        for preset_name, bucket, obs_cfg_path, dst_root in download_jobs:
            for pfx in prefixes_to_download:
                _log(logger, log_mode, f"[download] OBS download: preset={preset_name} bucket={bucket} prefix={pfx} dst={dst_root} config={obs_cfg_path}")
                _obsutil_cp_prefix(
                    obsutil_exe=obsutil_exe,
                    obsutil_config=obs_cfg_path,
                    bucket=bucket,
                    obs_prefix=pfx,
                    dst_root=dst_root,
                    parallel=parallel,
                    jobs=jobs,
                    force=force,
                    dry_run=dry_run,
                    logger=logger,
                )
    else:
        _log(logger, log_mode, "[download] skip_download=True -> 跳过下载，仅做搬运/分组")

    # -------------------------
    # 3) 收集“来源目录”
    # -------------------------
    # sources 的语义：可能包含“容器目录”或“task_dir”
    # prod_all：优先 sources 来自 staging
    sources: List[Tuple[Path, Optional[str]]] = []

    # (a) obs_download 若存在，也作为来源（可能未分组）
    if obs_download_root.exists() and obs_download_root.is_dir():
        sources.append((obs_download_root, None))

    if current_preset in ("prod_all", "all"):
        # staging：每个 preset 的 dst_root/collect 作为来源
        for preset_name, _bucket, _cfg, dst_root in download_jobs:
            staging_collect = dst_root / "collect"
            if staging_collect.exists() and staging_collect.is_dir():
                sources.append((staging_collect, preset_name))
    else:
        # (b) 非 CSV：只需要考虑 repo_root/collect
        if (not csv_mode) and collect_root.exists() and collect_root.is_dir():
            if not obs_download_root.exists() or collect_root.resolve() != obs_download_root.resolve():
                sources.append((collect_root, None))

        # (c) CSV：每个 prefix 的 leaf 落盘目录（如果存在）
        if csv_mode:
            for pfx in prefixes_to_download:
                lf = _basename_of_prefix(pfx)
                p = repo_root / lf
                if p.exists() and p.is_dir():
                    try:
                        if p.resolve() == obs_download_root.resolve():
                            continue
                    except Exception:
                        pass
                    try:
                        if collect_root.exists() and p.resolve() == collect_root.resolve():
                            continue
                    except Exception:
                        pass
                    sources.append((p, None))

    if not sources:
        raise FileNotFoundError(
            f"本地未找到可用数据目录：既没有 {obs_download_root}，"
            f"{'也没有 ' + str(collect_root) if (not csv_mode and current_preset not in ('prod_all', 'all')) else ''}，"
            f"也没有 staging/CSV leaf 目录。 (skip_download={skip_download}, csv_mode={csv_mode}, current_preset={current_preset})"
        )

    if not dry_run:
        obs_download_root.mkdir(parents=True, exist_ok=True)

    # -------------------------
    # 4) 搬运：taskid -> obs_download/<model>/<taskid>
    # -------------------------
    model_set: set[str] = set()

    for src, source_preset in sources:
        # 方法1：非 CSV 模式下，collect_root 一定是“容器目录”，禁止把它当作 task_dir 整体搬走
        if (current_preset not in ("prod_all", "all")) and (not csv_mode) and (src.resolve() == collect_root.resolve()):
            _log(logger, log_mode, f"[download] non-csv: treat collect_root as container only: {src}")

            task_dirs = _list_direct_task_dirs(src, taskid_re=taskid_re, filter_taskid_dirs=filter_taskid_dirs)
            if not task_dirs:
                _log(logger, log_mode, f"[download] no direct taskid dirs found in collect_root: {src} (skip)")
                continue

            _log(logger, log_mode, f"[download] found direct taskid dirs: {len(task_dirs)} in {src}")
            for task_dir in task_dirs:
                moved_model = _move_one_taskdir_into_model(
                    task_dir=task_dir,
                    obs_download_root=obs_download_root,
                    default_model=default_model,
                    illegal_win_re=illegal_win_re,
                    move_retries=move_retries,
                    dry_run=dry_run,
                    logger=logger,
                    log_mode=log_mode,
                    source_preset=source_preset,
                )
                if moved_model:
                    model_set.add(moved_model)
            continue

        # 方法2：prod_all/all 模式下，staging_collect 一定是"容器目录"，强制按容器处理
        if (current_preset in ("prod_all", "all")) and (source_preset is not None):
            _log(logger, log_mode, f"[download] {current_preset}: treat staging_collect as container only: {src} (source_preset={source_preset})")

            task_dirs = _list_direct_task_dirs(src, taskid_re=taskid_re, filter_taskid_dirs=filter_taskid_dirs)
            if not task_dirs:
                _log(logger, log_mode, f"[download] no direct taskid dirs found in staging_collect: {src} (skip)")
                continue

            _log(logger, log_mode, f"[download] found direct taskid dirs: {len(task_dirs)} in {src}")
            for task_dir in task_dirs:
                moved_model = _move_one_taskdir_into_model(
                    task_dir=task_dir,
                    obs_download_root=obs_download_root,
                    default_model=default_model,
                    illegal_win_re=illegal_win_re,
                    move_retries=move_retries,
                    dry_run=dry_run,
                    logger=logger,
                    log_mode=log_mode,
                    source_preset=source_preset,
                )
                if moved_model:
                    model_set.add(moved_model)
            continue

        # 情况1：src 本身就是一个 taskid 目录（常见于 CSV leaf）
        if _looks_like_task_dir(src, taskid_re=taskid_re):
            _log(logger, log_mode, f"[download] source is a task_dir: {src} (source_preset={source_preset})")
            moved_model = _move_one_taskdir_into_model(
                task_dir=src,
                obs_download_root=obs_download_root,
                default_model=default_model,
                illegal_win_re=illegal_win_re,
                move_retries=move_retries,
                dry_run=dry_run,
                logger=logger,
                log_mode=log_mode,
                source_preset=source_preset,
            )
            if moved_model:
                model_set.add(moved_model)
            continue

        # 情况2：src 是容器目录：只处理它“一层的 taskid 目录”
        task_dirs = _list_direct_task_dirs(src, taskid_re=taskid_re, filter_taskid_dirs=filter_taskid_dirs)

        # 如果 src 已经是“obs_download/<model>/<taskid>”这种结构，则它一层不会有 taskid
        if not task_dirs:
            if _looks_like_grouped_root(src, taskid_re=taskid_re):
                _log(logger, log_mode, f"[download] detected grouped root -> skip regroup: {src}")
                try:
                    for p in src.iterdir():
                        if p.is_dir():
                            model_set.add(p.name)
                except Exception:
                    pass
            else:
                _log(logger, log_mode, f"[download] no direct taskid dirs found in: {src} (skip)")
            continue

        _log(logger, log_mode, f"[download] found direct taskid dirs: {len(task_dirs)} in {src} (source_preset={source_preset})")

        for task_dir in task_dirs:
            moved_model = _move_one_taskdir_into_model(
                task_dir=task_dir,
                obs_download_root=obs_download_root,
                default_model=default_model,
                illegal_win_re=illegal_win_re,
                move_retries=move_retries,
                dry_run=dry_run,
                logger=logger,
                log_mode=log_mode,
                source_preset=source_preset,
            )
            if moved_model:
                model_set.add(moved_model)

    models = sorted(model_set)
    _log(logger, log_mode, f"[download] discovered models={models}")

    return DownloadResult(models=models, collect_root=collect_root, obs_download_root=obs_download_root)


# =============================================================================
# 核心：移动 task_dir -> obs_download/<model>/<taskid>
# =============================================================================

def _move_one_taskdir_into_model(
    *,
    task_dir: Path,
    obs_download_root: Path,
    default_model: str,
    illegal_win_re: re.Pattern,
    move_retries: int,
    dry_run: bool,
    logger,
    log_mode: str,
    source_preset: Optional[str],
) -> Optional[str]:
    taskid = task_dir.name

    # 已经在 obs_download/<model>/<taskid> 内的情况：跳过
    try:
        if task_dir.parent.parent.resolve() == obs_download_root.resolve():
            _log(logger, log_mode, f"[download] already grouped task -> skip: {task_dir}")
            return task_dir.parent.name
    except Exception:
        pass

    sample_json = _pick_sample_json_fast(task_dir)
    model = _detect_model_from_collect_json_fast(
        sample_json,
        default_model=default_model,
        logger=logger,
        log_mode=log_mode,
    )
    model = _sanitize_name(model, illegal_win_re=illegal_win_re, fallback=default_model)

    dst = obs_download_root / model / taskid

    try:
        if task_dir.resolve() == dst.resolve():
            _log(logger, log_mode, f"[download] src==dst -> skip: {task_dir}")
            return model
    except Exception:
        pass

    # 冲突必须报错（你的要求）
    if dst.exists():
        raise RuntimeError(
            f"[download] CONFLICT: dst already exists, must abort. "
            f"taskid={taskid} model={model} dst={dst} src={task_dir} source_preset={source_preset}"
        )

    _log(logger, log_mode, f"[download] MOVE task={taskid} model={model} -> {dst} (source_preset={source_preset})")

    if dry_run:
        return model

    dst.parent.mkdir(parents=True, exist_ok=True)
    _move_dir_with_retry(src=task_dir, dst=dst, retries=move_retries, logger=logger, log_mode=log_mode, tag="task_to_model")

    # 可选：落一个来源标记文件（你说没问题）
    if source_preset:
        _write_source_preset_marker(dst, source_preset=source_preset, logger=logger, log_mode=log_mode)

    return model


# =============================================================================
# 内部工具函数
# =============================================================================

def _ensure_trailing_slash(p: str) -> str:
    s = (p or "").strip()
    if not s:
        return ""
    return s if s.endswith("/") else (s + "/")


def _basename_of_prefix(obs_prefix: str) -> str:
    s = (obs_prefix or "").strip().strip("/")
    if not s:
        return "collect"
    parts = [p for p in s.split("/") if p]
    return parts[-1] if parts else "collect"


def _log(logger, log_mode: str, msg: str) -> None:
    if logger is None:
        print(msg)
        return
    logger.info(msg)


def _rmtree_force(p: Path) -> None:
    if not p.exists():
        return

    def _onerror(func, path, exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
        except Exception:
            pass
        try:
            func(path)
        except Exception:
            pass

    try:
        shutil.rmtree(p, onerror=_onerror)
    except Exception:
        pass


def _obsutil_cp_prefix(
    obsutil_exe: Optional[str],
    obsutil_config: str,
    bucket: str,
    obs_prefix: str,
    dst_root: Path,
    parallel: int,
    jobs: int,
    force: bool,
    dry_run: bool,
    logger,
) -> None:
    exe = obsutil_exe or "obsutil"
    src = f"obs://{bucket}/{obs_prefix.lstrip('/')}"
    dst = str(dst_root)

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
        f"-config={obsutil_config}",
    ]
    if force:
        cmd.append("-f")

    if dry_run:
        if logger:
            logger.info("[download] DRY_RUN obsutil cmd: %s", " ".join(cmd))
        else:
            print("[download] DRY_RUN obsutil cmd:", " ".join(cmd))
        return

    dst_root.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"obsutil cp failed, returncode={proc.returncode}, cmd={' '.join(cmd)}")


def _looks_like_task_dir(p: Path, taskid_re: re.Pattern) -> bool:
    try:
        if taskid_re.match(p.name or ""):
            return True
    except Exception:
        pass

    try:
        with os.scandir(p) as it:
            for e in it:
                if e.is_dir():
                    return True
    except Exception:
        return False
    return False


def _list_direct_task_dirs(root: Path, taskid_re: re.Pattern, filter_taskid_dirs: bool) -> List[Path]:
    children: List[Path] = []
    try:
        with os.scandir(root) as it:
            for e in it:
                if e.is_dir():
                    children.append(Path(e.path))
    except FileNotFoundError:
        return []

    matched = [p for p in children if taskid_re.match(p.name or "")]
    if filter_taskid_dirs:
        return matched

    return matched


def _looks_like_grouped_root(root: Path, taskid_re: re.Pattern) -> bool:
    try:
        children = [p for p in root.iterdir() if p.is_dir()]
    except Exception:
        return False

    if any(taskid_re.match(p.name or "") for p in children):
        return False

    for mdir in children[: min(5, len(children))]:
        try:
            with os.scandir(mdir) as it:
                for e in it:
                    if e.is_dir() and taskid_re.match(e.name or ""):
                        return True
        except Exception:
            continue

    return False


def _pick_sample_json_fast(task_dir: Path) -> Optional[Path]:
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

    try:
        for p in first_ep.glob("*_collect.json"):
            return p
        for p in first_ep.glob("*.json"):
            return p
    except FileNotFoundError:
        return None
    return None


def _detect_model_from_collect_json_fast(sample_json: Optional[Path], default_model: str, logger, log_mode: str) -> str:
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

    v2 = _deep_find_first_key(data, keys={"model", "robotModel"})
    if isinstance(v2, str) and v2.strip():
        return v2.strip()

    return default_model


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
    s = (name or "").strip()
    if not s:
        return fallback
    s = illegal_win_re.sub("_", s)
    s = s.strip(" .")
    return s or fallback


def _move_dir_with_retry(src: Path, dst: Path, retries: int, logger, log_mode: str, tag: str) -> None:
    last_err: Optional[Exception] = None
    attempts = max(1, int(retries) if retries else 1)

    for i in range(1, attempts + 1):
        try:
            src.rename(dst)
            return
        except PermissionError as e:
            last_err = e
            _log(logger, log_mode, f"[download] PermissionError move retry {i}/{attempts} tag={tag} src={src} dst={dst}")
            time.sleep(min(0.35 * i, 5.0))
        except OSError as e:
            last_err = e
            _log(logger, log_mode, f"[download] OSError move retry {i}/{attempts} tag={tag} src={src} dst={dst} err={e}")
            time.sleep(min(0.35 * i, 5.0))

    try:
        shutil.move(str(src), str(dst))
    except Exception as e:
        raise RuntimeError(f"Move failed tag={tag} src={src} dst={dst} last_err={last_err} final_err={e}") from e


def _read_csv_rows(
    *,
    repo_root: Path,
    csv_path: str,
    col_name: Optional[str],
    col_index: int,
    skip_header: bool,
    logger,
    log_mode: str,
) -> List[str]:
    p = Path(csv_path)
    if not p.is_absolute():
        p = repo_root / p

    if not p.exists():
        raise FileNotFoundError(f"csv_path not found: {p}")

    rows: List[str] = []
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        if col_name is not None:
            reader = csv.DictReader(f)
            for r in reader:
                v = (r.get(col_name) or "").strip()
                if v:
                    rows.append(v)
        else:
            reader2 = csv.reader(f)
            for i, r in enumerate(reader2):
                if i == 0 and skip_header:
                    continue
                if col_index < 0 or col_index >= len(r):
                    continue
                v = (r[col_index] or "").strip()
                if v:
                    rows.append(v)

    _log(logger, log_mode, f"[download] csv rows loaded: {len(rows)} from {p}")
    return rows


def _write_source_preset_marker(task_root: Path, source_preset: str, logger, log_mode: str) -> None:
    """
    在 obs_download/<model>/<taskid> 下写一个标记文件，方便排查来源。
    这个文件不参与任何逻辑判断（download 阶段也不依赖它）。
    """
    try:
        marker = task_root / ".source_preset.json"
        data = {"preset": source_preset}
        marker.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        _log(logger, log_mode, f"[download] wrote source preset marker: {marker}")
    except Exception as e:
        _log(logger, log_mode, f"[download] write source preset marker failed: task={task_root} err={e}")
