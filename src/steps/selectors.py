# src/steps/selectors.py
# -*- coding: utf-8 -*-
"""
selectors step
职责：
1) 从 obs_download/<model>/... 的 json 生成“字段 selector 列表”
2) 应用过滤规则（angle_allowlist / charseq_allowlist / angle_blocklist 等）
3) 可选随机一致性检查：抽样多个 task，比较 selector 集合是否一致
4) 输出：repo_root/csv_output/<model>/<model>_selectors.txt
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class SelectorsResult:
    model_to_selectors_txt: Dict[str, Path]

def _escape_angle_value(x: Any) -> str:
    if x is None:
        s = "null"
    elif isinstance(x, bool):
        s = "true" if x else "false"
    elif isinstance(x, (int, float)) and not isinstance(x, bool):
        s = str(x)
    elif isinstance(x, str):
        s = x
    else:
        try:
            s = json.dumps(x, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            s = str(x)
    return s.replace("\\", "\\\\").replace(">", "\\>")


def _token(name: Any) -> str:
    return f"<{_escape_angle_value(name)}>"


def _is_leaf(x: Any) -> bool:
    if x is None or isinstance(x, (str, int, float, bool)):
        return True
    if isinstance(x, list):
        return all(not isinstance(i, dict) for i in x)
    return False


def _first_key(d: dict) -> Optional[str]:
    for k in d.keys():
        return str(k)
    return None


# =============================================================================
# step 入口
# =============================================================================

def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> SelectorsResult:
    logger = runtime.get("logger")
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode", "normal"))
    obs_download_root: Path = runtime["obs_download_root"]
    models: List[str] = runtime["models"]

    csv_output_dir = repo_root / "csv_output"
    csv_output_dir.mkdir(parents=True, exist_ok=True)

    # 读取配置
    angle_allowlist: List[str] = list(step_cfg.get("angle_allowlist"))
    charseq_allowlist: List[List[str]] = list(step_cfg.get("charseq_allowlist"))
    charseq_match_mode: str = str(step_cfg.get("charseq_match_mode", "both"))
    charseq_case_sensitive: bool = bool(step_cfg.get("charseq_case_sensitive"))

    angle_blocklist: List[str] = list(step_cfg.get("angle_blocklist"))

    filter_order: List[str] = list(step_cfg.get("filter_order"))
    angle_match_mode: str = str(step_cfg.get("angle_match_mode"))
    angle_block_match_mode: str = str(step_cfg.get("angle_block_match_mode"))

    enable_filter: bool = bool(step_cfg.get("enable_filter"))
    enable_angle_allow_add: bool = bool(step_cfg.get("enable_angle_allow_add"))
    enable_charseq_allow_add: bool = bool(step_cfg.get("enable_charseq_allow_add"))
    enable_angle_block_exclude: bool = bool(step_cfg.get("enable_angle_block_exclude"))
    allow_all_if_no_allowlists: bool = bool(step_cfg.get("allow_all_if_no_allowlists"))

    enable_random_consistency_check: bool = bool(step_cfg.get("enable_random_consistency_check"))
    consistency_sample_tasks: int = int(step_cfg.get("consistency_sample_tasks"))
    consistency_random_seed = step_cfg.get("consistency_random_seed")
    consistency_diff_preview: int = int(step_cfg.get("consistency_diff_preview"))

    model_to_txt: Dict[str, Path] = {}

    for model in models:
        model_root = obs_download_root / model
        if not model_root.exists():
            _log(logger, log_mode, f"[selectors] model={model} 不存在目录：{model_root} -> 跳过")
            continue

        # 1) 选一个样本 json（优先 *_collect.json）
        sample_json = _pick_one_json(model_root)
        if sample_json is None:
            _log(logger, log_mode, f"[selectors] model={model} 未找到 json -> 跳过")
            continue

        # 2) 生成 selectors
        raw_selectors = _generate_selectors_from_json(sample_json, logger=logger, log_mode=log_mode)
        _log(logger, log_mode, f"[selectors] model={model} raw selectors: {len(raw_selectors)} (sample={sample_json})")

        # 3) 过滤
        if enable_filter:
            filtered = _apply_filters(
                selectors=raw_selectors,
                angle_allowlist=angle_allowlist,
                charseq_allowlist=charseq_allowlist,
                angle_blocklist=angle_blocklist,
                filter_order=filter_order,
                angle_match_mode=angle_match_mode,
                angle_block_match_mode=angle_block_match_mode,
                charseq_match_mode=charseq_match_mode,
                charseq_case_sensitive=charseq_case_sensitive,
                enable_angle_allow_add=enable_angle_allow_add,
                enable_charseq_allow_add=enable_charseq_allow_add,
                enable_angle_block_exclude=enable_angle_block_exclude,
                allow_all_if_no_allowlists=allow_all_if_no_allowlists,
            )
        else:
            filtered = sorted(set(raw_selectors))

        _log(logger, log_mode, f"[selectors] model={model} filtered selectors: {len(filtered)}")

        # 4) 随机一致性检查
        if enable_random_consistency_check:
            _random_consistency_check(
                model=model,
                model_root=model_root,
                base_selectors=set(filtered),
                sample_tasks=consistency_sample_tasks,
                seed=consistency_random_seed,
                diff_preview=consistency_diff_preview,
                logger=logger,
                log_mode=log_mode,
            )

        # 5) 写 selectors.txt
        out_dir = csv_output_dir / model
        out_dir.mkdir(parents=True, exist_ok=True)
        out_txt = out_dir / f"{model}_selectors.txt"
        out_txt.write_text("\n".join(filtered) + "\n", encoding="utf-8")
        model_to_txt[model] = out_txt

    return SelectorsResult(model_to_selectors_txt=model_to_txt)


# =============================================================================
# selector 生成
# =============================================================================

def _generate_selectors_from_json(sample_json: Path, logger, log_mode: str) -> List[str]:
    try:
        data = json.loads(sample_json.read_text(encoding="utf-8-sig", errors="ignore"))
    except Exception as e:
        _log(logger, log_mode, f"[selectors] 解析 json 失败：{sample_json} err={e}")
        return []

    out: List[str] = []
    dedup: Set[str] = set()

    def walk(node: Any, selector_prefix: str) -> None:
        if _is_leaf(node):
            if selector_prefix and selector_prefix not in dedup:
                dedup.add(selector_prefix)
                out.append(selector_prefix)
            return

        if isinstance(node, dict):
            for k, v in node.items():
                child_selector = f"{selector_prefix}.{_token(str(k))}" if selector_prefix else f".{_token(str(k))}"
                if _is_leaf(v):
                    if child_selector in dedup:
                        continue
                    dedup.add(child_selector)
                    out.append(child_selector)
                else:
                    walk(v, child_selector)
            return

        if isinstance(node, list):
            # 只遍历 list 里的 dict；filter key = dict 的第一个 key
            for item in node:
                if not isinstance(item, dict):
                    continue
                fk = _first_key(item)
                if fk is None:
                    continue
                fv = item.get(fk)
                filt = f".[<{_escape_angle_value(fk)}>=" f"<{_escape_angle_value(fv)}>]"
                child_selector = f"{selector_prefix}{filt}" if selector_prefix else f"{filt}"
                walk(item, child_selector)
            return

    walk(data, selector_prefix="")

    return sorted(set(out))


# =============================================================================
# 过滤器
# =============================================================================

_ANGLE_RE = re.compile(r"<([^<>]+)>")


def _apply_filters(
    selectors: List[str],
    angle_allowlist: List[str],
    charseq_allowlist: List[List[str]],
    angle_blocklist: List[str],
    filter_order: List[str],
    angle_match_mode: str,
    angle_block_match_mode: str,
    charseq_match_mode: str,
    charseq_case_sensitive: bool,
    enable_angle_allow_add: bool,
    enable_charseq_allow_add: bool,
    enable_angle_block_exclude: bool,
    allow_all_if_no_allowlists: bool,
) -> List[str]:
    all_set = set(selectors)

    allow_set: Set[str] = set()
    if enable_angle_allow_add and angle_allowlist:
        for s in all_set:
            if _match_angle_allow(s, angle_allowlist, angle_match_mode):
                allow_set.add(s)

    if enable_charseq_allow_add and charseq_allowlist:
        for s in all_set:
            if _match_charseq_allow(s, charseq_allowlist, charseq_match_mode, charseq_case_sensitive):
                allow_set.add(s)

    if (not angle_allowlist) and (not charseq_allowlist):
        base_set = all_set if allow_all_if_no_allowlists else set()
    else:
        base_set = allow_set

    if enable_angle_block_exclude and angle_blocklist:
        base_set = {s for s in base_set if not _match_angle_allow(s, angle_blocklist, angle_block_match_mode)}

    return sorted(base_set)


def _match_angle_allow(selector: str, allowlist: List[str], mode: str) -> bool:
    angles = _ANGLE_RE.findall(selector)
    if mode == "contains":
        for a in angles:
            for w in allowlist:
                if w in a:
                    return True
        return any(w in selector for w in allowlist)
    return any(a == w for a in angles for w in allowlist)


def _match_charseq_allow(selector: str, rules: List[List[str]], mode: str, case_sensitive: bool) -> bool:
    s = selector if case_sensitive else selector.lower()

    for segs in rules:
        if not segs:
            continue
        parts = segs if case_sensitive else [x.lower() for x in segs]

        if mode == "adjacent":
            if "".join(parts) in s:
                return True
        elif mode == "ordered":
            pos = 0
            ok = True
            for p in parts:
                idx = s.find(p, pos)
                if idx < 0:
                    ok = False
                    break
                pos = idx + len(p)
            if ok:
                return True
        else:
            if all(p in s for p in parts):
                return True

    return False


# =============================================================================
# 随机一致性检查
# =============================================================================

def _random_consistency_check(
    model: str,
    model_root: Path,
    base_selectors: Set[str],
    sample_tasks: int,
    seed,
    diff_preview: int,
    logger,
    log_mode: str,
) -> None:
    tasks = [p for p in model_root.iterdir() if p.is_dir()]
    if not tasks:
        return

    rnd = random.Random(seed) if seed is not None else random.Random()
    rnd.shuffle(tasks)
    tasks = tasks[: min(sample_tasks, len(tasks))]

    for task_dir in tasks:
        sample = _pick_one_json(task_dir)
        if sample is None:
            continue
        s2 = set(_generate_selectors_from_json(sample, logger, log_mode))
        if s2 != base_selectors:
            only_a = sorted(base_selectors - s2)[:diff_preview]
            only_b = sorted(s2 - base_selectors)[:diff_preview]
            _log(logger, log_mode, f"[selectors][CONSISTENCY] model={model} task={task_dir.name} mismatch!")
            _log(logger, log_mode, f"  only_in_base({len(base_selectors - s2)}): {only_a}")
            _log(logger, log_mode, f"  only_in_sample({len(s2 - base_selectors)}): {only_b}")
            raise RuntimeError(f"Selectors consistency check failed: model={model}, task={task_dir.name}")


def _pick_one_json(root: Path) -> Optional[Path]:
    cands = [p for p in root.rglob("*.json") if p.is_file()]
    if not cands:
        return None
    collect_like = [p for p in cands if p.name.endswith("_collect.json")]
    return sorted(collect_like)[0] if collect_like else sorted(cands)[0]


def _log(logger, log_mode: str, msg: str) -> None:
    if logger is None:
        print(msg)
        return
    logger.info(msg)
