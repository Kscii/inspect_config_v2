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

collect_root 规则（单前缀模式）：
- collect_root = repo_root / basename(obs_prefix)
  - obs_prefix=data-collector-svc/collect/ -> collect_root=repo_root/collect
  - obs_prefix=data-collector-svc/collect/<taskid>/ -> collect_root=repo_root/<taskid>

重要变更（按你的要求）：
- 删除所有“重命名目录（collect->obs_download）”相关代码
- 改为和老实现一致：直接移动 taskid 目录到 obs_download/<model>/<taskid>

CSV 模式（csv_mode=true）：
- 读取 csv_path
- 每行：
  - 若 csv_rows_are_taskid=true：行值当作 taskid，拼为 obs_prefix + taskid + "/"
  - 否则：行值当作完整 prefix
- 对每个 prefix 执行 obsutil cp -> repo_root
- 下载后落盘目录通常是 repo_root/basename(prefix)，若 basename 看起来像 taskid，则把该目录当作 task_dir 直接搬入
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
from typing import Any, Dict, List, Optional


@dataclass
class DownloadResult:
    """download 产出：自动发现的构型列表（后续 steps 依赖）"""
    models: List[str]
    collect_root: Path
    obs_download_root: Path


_MODEL_RE = re.compile(r'"model"\s*:\s*"([^"]+)"', re.IGNORECASE)
_ROBOT_MODEL_RE = re.compile(r'"robotModel"\s*:\s*"([^"]+)"', re.IGNORECASE)


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
    # 读取 presets / bucket
    # -------------------------
    presets = global_cfg["presets"]
    current_preset = step_cfg["current_preset"]
    bucket = presets[current_preset]["obs_bucket"]

    obs_prefix = str(step_cfg["obs_prefix"])
    obs_prefix_norm = obs_prefix.lstrip("/")

    # -------------------------
    # 本地目录命名
    # -------------------------
    obs_download_rootname = str(step_cfg.get("obs_download_rootname"))
    obs_download_root = repo_root / obs_download_rootname

    # 单前缀模式的 collect_root = repo_root/<basename(obs_prefix)>
    leaf = _basename_of_prefix(obs_prefix)
    collect_root = repo_root / leaf

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

    _log(logger, log_mode, f"[download] resolved obs_download_root={obs_download_root}")
    _log(logger, log_mode, f"[download] resolved collect_root={collect_root} (leaf={leaf}, obs_prefix={obs_prefix})")
    _log(logger, log_mode, f"[download] csv_mode={csv_mode}")

    # -------------------------
    # 0) full_refresh：只在一开始清理（后面不再做任何删除）
    # -------------------------
    if full_refresh:
        # 注意：CSV 模式下可能落盘到多个叶子目录，这里只清理：
        # - obs_download_root（统一产物）
        # - 单前缀模式推导的 collect_root（保持兼容）
        _log(logger, log_mode, f"[download] full_refresh=True -> 清理 {collect_root} 和 {obs_download_root}")
        if not dry_run:
            _rmtree_force(collect_root)
            _rmtree_force(obs_download_root)

    # -------------------------
    # 1) 下载（可选）
    # -------------------------
    prefixes_to_download: List[str] = []
    if csv_mode:
        # base prefix 用于拼接（当 csv_rows_are_taskid=True）
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
            # base_prefix + taskid + "/"
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

    if not skip_download:
        for pfx in prefixes_to_download:
            _log(logger, log_mode, f"[download] OBS download: bucket={bucket}, prefix={pfx}, dst={repo_root}")
            _obsutil_cp_prefix_to_repo_root(
                obsutil_exe=obsutil_exe,
                bucket=bucket,
                obs_prefix=pfx,
                repo_root=repo_root,
                parallel=parallel,
                jobs=jobs,
                force=force,
                dry_run=dry_run,
                logger=logger,
            )
    else:
        _log(logger, log_mode, "[download] skip_download=True -> 跳过下载，仅做搬运/分组")

    # -------------------------
    # 2) 收集“来源目录/来源task”
    #
    # 目标：把所有可见的 taskid 目录都搬到：
    #   obs_download/<model>/<taskid>
    #
    # 来源可能包括：
    # - repo/obs_download 下直接有 taskid（一层未分组） -> 补分组
    # - 单前缀：collect_root（repo/collect 或 repo/<taskid>）
    # - CSV 模式：对每个 prefix 的 basename(prefix) -> repo_root/<leaf>（通常是 taskid）
    # -------------------------
    sources: List[Path] = []

    # (a) obs_download 若存在，也作为来源（可能未分组）
    if obs_download_root.exists() and obs_download_root.is_dir():
        sources.append(obs_download_root)

    # (b) 单前缀 collect_root 若存在
    if collect_root.exists() and collect_root.is_dir():
        if not obs_download_root.exists() or collect_root.resolve() != obs_download_root.resolve():
            sources.append(collect_root)

    # (c) CSV 模式：每个 prefix 的 leaf 目录（如果存在）
    if csv_mode:
        for pfx in prefixes_to_download:
            lf = _basename_of_prefix(pfx)
            p = repo_root / lf
            if p.exists() and p.is_dir():
                # 避免重复塞入 obs_download_root / collect_root
                if p.resolve() == obs_download_root.resolve():
                    continue
                if collect_root.exists() and p.resolve() == collect_root.resolve():
                    continue
                sources.append(p)

    if not sources:
        raise FileNotFoundError(
            f"本地未找到可用数据目录：既没有 {obs_download_root} 也没有 {collect_root}，"
            f"也没有任何 CSV leaf 目录。 (skip_download={skip_download}, csv_mode={csv_mode})"
        )

    if not dry_run:
        obs_download_root.mkdir(parents=True, exist_ok=True)

    # -------------------------
    # 3) 对每个来源，把 taskid 目录直接搬到 obs_download/<model>/<taskid>
    # -------------------------
    model_set: set[str] = set()

    for src in sources:
        # 情况1：src 本身就是一个 taskid 目录（常见于 obs_prefix=.../<taskid>/ 或 CSV leaf）
        if _looks_like_task_dir(src, taskid_re=taskid_re):
            _log(logger, log_mode, f"[download] source is a task_dir: {src}")
            moved_model = _move_one_taskdir_into_model(
                task_dir=src,
                obs_download_root=obs_download_root,
                default_model=default_model,
                illegal_win_re=illegal_win_re,
                move_retries=move_retries,
                dry_run=dry_run,
                logger=logger,
                log_mode=log_mode,
            )
            if moved_model:
                model_set.add(moved_model)
            continue

        # 情况2：src 是容器目录：只处理它“一层的 taskid 目录”
        task_dirs = _list_direct_task_dirs(src, taskid_re=taskid_re, filter_taskid_dirs=filter_taskid_dirs)

        # 如果 src 已经是“obs_download/<model>/<taskid>”这种结构，则它一层不会有 taskid
        # 这时跳过（避免把 model 当 task 去搬）
        if not task_dirs:
            if _looks_like_grouped_root(src, taskid_re=taskid_re):
                _log(logger, log_mode, f"[download] detected grouped root -> skip regroup: {src}")
                # 记录已有 models（仅用于返回）
                try:
                    for p in src.iterdir():
                        if p.is_dir():
                            model_set.add(p.name)
                except Exception:
                    pass
            else:
                _log(logger, log_mode, f"[download] no direct taskid dirs found in: {src} (skip)")
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
            )
            if moved_model:
                model_set.add(moved_model)

    models = sorted(model_set)
    _log(logger, log_mode, f"[download] discovered models={models}")

    # collect_root/obs_download_root 字段：为了兼容后续 steps
    # pipeline 应读取的根——永远是 obs_download_root
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

    # 源和目标完全一致：跳过
    try:
        if task_dir.resolve() == dst.resolve():
            _log(logger, log_mode, f"[download] src==dst -> skip: {task_dir}")
            return model
    except Exception:
        pass

    # 目标已存在：按老实现风格，跳过（不覆盖）
    if dst.exists():
        _log(logger, log_mode, f"[download] dst exists -> skip: {dst}")
        return model

    _log(logger, log_mode, f"[download] MOVE task={taskid} -> {dst}")

    if dry_run:
        return model

    dst.parent.mkdir(parents=True, exist_ok=True)
    _move_dir_with_retry(src=task_dir, dst=dst, retries=move_retries, logger=logger, log_mode=log_mode, tag="task_to_model")

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
    """
    取 obs_prefix 的“最下层路径段”作为本地下载目录名：
    - "data-collector-svc/collect/" -> "collect"
    - "data-collector-svc/collect/0343.../" -> "0343..."
    - "data-collector-svc/collect/0343..." -> "0343..."
    """
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


def _is_windows() -> bool:
    return os.name == "nt"


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


def _looks_like_task_dir(p: Path, taskid_re: re.Pattern) -> bool:
    """
    判断 p 是否“更像 task 目录本身”：
    - 名字匹配 taskid_regex（强判定）
    - 或者内部有 episode 子目录（弱判定，避免未来 taskid_regex 变化）
    """
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
    """
    只列出 root 下一层的 taskid 目录，避免把 model 目录当 task 搬走。
    - filter_taskid_dirs=True：严格只取匹配 taskid_regex 的目录
    - filter_taskid_dirs=False：若能找到匹配 taskid_regex 的目录，就只用这些；否则返回空（宁可不搬，避免误搬）
    """
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
    """
    判断 root 是否已经是 obs_download/<model>/<taskid> 结构：
    - root 下一层没有 taskid
    - 但 root 下一层的某些目录内部有 taskid 子目录
    """
    try:
        children = [p for p in root.iterdir() if p.is_dir()]
    except Exception:
        return False

    direct_task = any(taskid_re.match(p.name or "") for p in children)
    if direct_task:
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
    1) regex 抓 "model":"xxx" / "robotModel":"xxx"
    2) json.loads + 深搜 key
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
    """清洗文件夹名，保证 Windows/跨平台兼容。"""
    s = (name or "").strip()
    if not s:
        return fallback
    s = illegal_win_re.sub("_", s)
    s = s.strip(" .")
    return s or fallback


def _move_dir_with_retry(src: Path, dst: Path, retries: int, logger, log_mode: str, tag: str) -> None:
    """
    与老实现一致：
    - 同盘优先 src.rename(dst)（速度最快）
    - PermissionError 重试（适配 Defender/索引器占用）
    - 最后 fallback 到 shutil.move
    """
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

    # fallback
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
    """
    读取 CSV，返回每行的目标字段字符串。
    - csv_path 支持相对 repo_root 或绝对路径
    - col_name 非空：按表头列名取值
    - col_name 为空/None：按 col_index 取值（0-based）
    - skip_header：仅当无表头但第一行是说明时用（会跳过第一行）
    """
    p = Path(csv_path)
    if not p.is_absolute():
        p = repo_root / p

    if not p.exists():
        raise FileNotFoundError(f"csv_path not found: {p}")

    rows: List[str] = []
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        if col_name is not None:
            reader = csv.DictReader(f)
            for i, r in enumerate(reader):
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
