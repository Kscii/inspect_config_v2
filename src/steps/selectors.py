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
from core.pipeline import filter_json_files_by_area

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
    # consistency_sample_tasks 支持 "all" 或整数
    consistency_sample_tasks_raw = step_cfg.get("consistency_sample_tasks")
    consistency_sample_tasks = consistency_sample_tasks_raw  # 保留原始值（"all" 或整数）
    consistency_random_seed = step_cfg.get("consistency_random_seed")
    consistency_diff_preview: int = int(step_cfg.get("consistency_diff_preview"))

    use_union_of_all_samples: bool = bool(step_cfg.get("use_union_of_all_samples", False))
    enable_union_min_sample_filter: bool = bool(step_cfg.get("enable_union_min_sample_filter", False))
    union_min_sample_ratio: float = float(step_cfg.get("union_min_sample_ratio", 0.8))
    union_min_sample_ratio_by_model: Dict[str, float] = dict(step_cfg.get("union_min_sample_ratio_by_model", {}))

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
                model=model,  # 传递当前构型
            )
        else:
            filtered = sorted(set(raw_selectors))

        _log(logger, log_mode, f"[selectors] model={model} filtered selectors: {len(filtered)}")

        # 4) 可选：使用所有任务的并集
        if use_union_of_all_samples:
            # 确定当前构型的最小样本比例阈值
            min_sample_ratio = union_min_sample_ratio_by_model.get(model, union_min_sample_ratio)
            
            union_selectors = _compute_union_of_all_samples(
                model=model,
                model_root=model_root,
                logger=logger,
                log_mode=log_mode,
                enable_filter=enable_filter,
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
                enable_min_sample_filter=enable_union_min_sample_filter,
                min_sample_ratio=min_sample_ratio,
                runtime=runtime,
            )
            _log(logger, log_mode, f"[selectors] model={model} union selectors from all tasks: {len(union_selectors)}")
            filtered = sorted(union_selectors)

        # 5) 随机一致性检查
        if enable_random_consistency_check:
            _random_consistency_check(
                model=model,
                model_root=model_root,
                base_sample_path=sample_json,  # 传递 base 样本路径
                base_selectors=set(filtered),
                sample_tasks=consistency_sample_tasks,
                seed=consistency_random_seed,
                diff_preview=consistency_diff_preview,
                logger=logger,
                log_mode=log_mode,
                # 传递过滤参数，确保一致性检查也使用相同的过滤规则
                enable_filter=enable_filter,
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

        # 6) 写 selectors.txt
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
# 求并集逻辑
# =============================================================================

def _compute_union_of_all_samples(
    model: str,
    model_root: Path,
    logger,
    log_mode: str,
    enable_filter: bool,
    angle_allowlist: List[str],
    charseq_allowlist: List[List[str]],
    angle_blocklist: List[Any],
    filter_order: List[str],
    angle_match_mode: str,
    angle_block_match_mode: str,
    charseq_match_mode: str,
    charseq_case_sensitive: bool,
    enable_angle_allow_add: bool,
    enable_charseq_allow_add: bool,
    enable_angle_block_exclude: bool,
    allow_all_if_no_allowlists: bool,
    enable_min_sample_filter: bool = False,
    min_sample_ratio: float = 0.8,
    runtime: Optional[Dict[str, Any]] = None,
) -> Set[str]:
    """
    遍历 model_root 下的所有任务目录，对每个任务：
    1. 生成原始 selectors
    2. 应用过滤规则
    3. 求并集
    4. 可选：过滤出现比例低的字段
    
    返回：所有任务过滤后 selectors 的并集（可选经过最小样本比例过滤）
    """
    union: Set[str] = set()
    # 统计每个 selector 在多少个样本中出现
    selector_count: Dict[str, int] = {} if enable_min_sample_filter else None
    
    tasks = [p for p in model_root.iterdir() if p.is_dir()]
    
    # 根据 enabled_areas 过滤任务目录
    if runtime and runtime.get("enabled_areas") is not None:
        enabled_areas = runtime["enabled_areas"]
        taskid_to_area = runtime.get("taskid_to_area", {})
        filtered_tasks = []
        for task_dir in tasks:
            area = taskid_to_area.get(task_dir.name)
            if area and area in enabled_areas:
                filtered_tasks.append(task_dir)
        tasks = filtered_tasks
        _log(logger, log_mode, f"[selectors][UNION] model={model} area过滤后剩余 {len(tasks)} 个任务目录")
    
    _log(logger, log_mode, f"[selectors][UNION] model={model} 开始遍历 {len(tasks)} 个任务目录")
    
    success_count = 0
    skip_count = 0
    
    for task_dir in tasks:
        sample_json = _pick_one_json(task_dir)
        if sample_json is None:
            skip_count += 1
            continue
        
        # 生成原始 selectors
        raw_selectors = _generate_selectors_from_json(sample_json, logger, log_mode)
        if not raw_selectors:
            skip_count += 1
            continue
        
        # 应用过滤规则
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
                model=model,
            )
        else:
            filtered = sorted(set(raw_selectors))
        
        # 求并集
        union.update(filtered)
        
        # 统计每个 selector 的出现次数
        if enable_min_sample_filter:
            for sel in filtered:
                selector_count[sel] = selector_count.get(sel, 0) + 1
        
        success_count += 1
    
    _log(logger, log_mode, f"[selectors][UNION] model={model} 完成：成功处理 {success_count} 个任务，跳过 {skip_count} 个任务")
    
    # 应用最小样本比例过滤
    if enable_min_sample_filter and selector_count and success_count > 0:
        original_count = len(union)
        # 计算比例：selector出现的样本数 / 总样本数 >= 阈值比例
        union = {sel for sel in union if selector_count.get(sel, 0) / success_count >= min_sample_ratio}
        filtered_out = original_count - len(union)
        _log(logger, log_mode, f"[selectors][UNION] model={model} 最小样本比例过滤：阈值={min_sample_ratio*100:.1f}% (总样本={success_count}), 排除字段={filtered_out}, 保留字段={len(union)}")
    
    return union


# =============================================================================
# 过滤器
# =============================================================================

_ANGLE_RE = re.compile(r"<([^<>]+)>")


def _apply_filters(
    selectors: List[str],
    angle_allowlist: List[str],
    charseq_allowlist: List[List[str]],
    angle_blocklist: List[Any],  # 改为 List[Any] 以支持新格式
    filter_order: List[str],
    angle_match_mode: str,
    angle_block_match_mode: str,
    charseq_match_mode: str,
    charseq_case_sensitive: bool,
    enable_angle_allow_add: bool,
    enable_charseq_allow_add: bool,
    enable_angle_block_exclude: bool,
    allow_all_if_no_allowlists: bool,
    model: str = "",  # 新增：当前构型名称
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
        # 使用新的黑名单匹配逻辑
        base_set = {s for s in base_set if not _match_blocklist_rules(s, angle_blocklist, model)}

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


def _match_blocklist_rules(selector: str, rules: List[Any], model: str) -> bool:
    """
    匹配新格式的黑名单规则
    规则格式：
    - models: ["MODEL1", "MODEL2"]  # 可选
      contains: ["str1", "str2"]      # 必选
    """
    if not rules:
        return False
    
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        
        # 检查 models 约束
        rule_models = rule.get("models", [])
        if rule_models and isinstance(rule_models, list):
            # 如果指定了 models，检查当前 model 是否在列表中（不区分大小写）
            rule_models_lower = [str(m).lower() for m in rule_models]
            if model.lower() not in rule_models_lower:
                continue
        
        # 检查 contains 约束
        contains = rule.get("contains", [])
        if not contains or not isinstance(contains, list):
            continue
        
        # 所有字符串都必须在 selector 中出现
        if all(str(c) in selector for c in contains):
            return True
    
    return False


# =============================================================================
# 随机一致性检查
# =============================================================================

def _random_consistency_check(
    model: str,
    model_root: Path,
    base_sample_path: Path,  # 新增：base 样本路径
    base_selectors: Set[str],
    sample_tasks,  # 支持 "all" 或整数
    seed,
    diff_preview: int,
    logger,
    log_mode: str,
    # 新增：过滤参数
    enable_filter: bool,
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
) -> None:
    tasks = [p for p in model_root.iterdir() if p.is_dir()]
    if not tasks:
        return

    # 如果 sample_tasks 是 "all"，检查所有任务；否则随机抽样
    if isinstance(sample_tasks, str) and sample_tasks.lower() == "all":
        _log(logger, log_mode, f"[selectors][CONSISTENCY] model={model} 使用全部 {len(tasks)} 个任务进行一致性检查")
        # 不进行随机抽样，使用所有任务
    else:
        sample_tasks_int = int(sample_tasks)
        rnd = random.Random(seed) if seed is not None else random.Random()
        rnd.shuffle(tasks)
        tasks = tasks[: min(sample_tasks_int, len(tasks))]
        _log(logger, log_mode, f"[selectors][CONSISTENCY] model={model} 随机抽样 {len(tasks)} 个任务进行一致性检查")

    for task_dir in tasks:
        sample = _pick_one_json(task_dir)
        if sample is None:
            continue
        
        # 生成原始selectors
        raw_selectors = _generate_selectors_from_json(sample, logger, log_mode)
        
        # 应用相同的过滤规则
        if enable_filter:
            filtered_selectors = _apply_filters(
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
                model=model,  # 传递当前构型
            )
            s2 = set(filtered_selectors)
        else:
            s2 = set(sorted(set(raw_selectors)))
        
        if s2 != base_selectors:
            only_a = sorted(base_selectors - s2)[:diff_preview]
            only_b = sorted(s2 - base_selectors)[:diff_preview]
            
            # 提取 base 和 sample 的 taskid
            base_taskid = _extract_taskid_from_path(base_sample_path, model_root)
            sample_taskid = task_dir.name
            
            _log(logger, log_mode, f"[selectors][CONSISTENCY] model={model} 检测到字段不一致!")
            _log(logger, log_mode, f"  BASE sample:")
            _log(logger, log_mode, f"    - taskid: {base_taskid}")
            _log(logger, log_mode, f"    - file: {base_sample_path}")
            _log(logger, log_mode, f"  CURRENT sample:")
            _log(logger, log_mode, f"    - taskid: {sample_taskid}")
            _log(logger, log_mode, f"    - file: {sample}")
            _log(logger, log_mode, f"  差异统计:")
            _log(logger, log_mode, f"    - only_in_base({len(base_selectors - s2)}): {only_a}")
            _log(logger, log_mode, f"    - only_in_sample({len(s2 - base_selectors)}): {only_b}")
            raise RuntimeError(f"Selectors consistency check failed: model={model}, base_task={base_taskid}, sample_task={sample_taskid}")


def _extract_taskid_from_path(file_path: Path, model_root: Path) -> str:
    """从文件路径中提取 taskid（假设 taskid 是目录结构中的某一层）"""
    try:
        # 尝试获取相对路径
        rel_path = file_path.relative_to(model_root)
        # taskid 通常是第一层目录
        parts = rel_path.parts
        if len(parts) > 0:
            return parts[0]
    except Exception:
        pass
    return "<unknown>"


def _pick_one_json(root: Path) -> Optional[Path]:
    cands = [p for p in root.rglob("*.json") if p.is_file() and p.name != ".source_preset.json"]
    if not cands:
        return None
    collect_like = [p for p in cands if p.name.endswith("_collect.json")]
    return sorted(collect_like)[0] if collect_like else sorted(cands)[0]


def _log(logger, log_mode: str, msg: str) -> None:
    if logger is None:
        print(msg)
        return
    logger.info(msg)
