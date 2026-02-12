"""
字段解析器模块
职责：
1) 从 JSON 生成 selector 列表（与 src/steps/selectors.py 保持一致：list 使用 first_key=first_value 过滤）
2) 提取字段值并判断数据类型
3) 生成 field_name 和 rule_code
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set

from ..utils import logger


# =============================================================================
# selector 生成（与 src/steps/selectors.py 对齐）
# =============================================================================

def _escape_angle_value(x: Any) -> str:
    """转义 angle bracket 中的值"""
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
    """生成 angle bracket token"""
    return f"<{_escape_angle_value(name)}>"


def _is_leaf(x: Any) -> bool:
    """判断是否为叶子节点"""
    if x is None or isinstance(x, (str, int, float, bool)):
        return True
    if isinstance(x, list):
        return all(not isinstance(i, dict) for i in x)
    return False


def _first_key(d: dict) -> Optional[str]:
    """获取字典的第一个键"""
    for k in d.keys():
        return str(k)
    return None


def _preferred_key(d: dict, preferred_keys: Optional[List[str]] = None) -> Optional[str]:
    """
    获取字典的优先键
    
    Args:
        d: 字典对象
        preferred_keys: 优先使用的键列表（按顺序）
        
    Returns:
        优先键，如果没有匹配则返回第一个键
    """
    if not preferred_keys:
        return _first_key(d)
    
    # 按优先级顺序检查
    for preferred in preferred_keys:
        if preferred in d:
            return preferred
    
    # 如果没有匹配的优先键，回退到第一个键
    return _first_key(d)


def generate_selectors_from_json(json_data: Any, preferred_filter_keys: Optional[List[str]] = None) -> List[str]:
    """
    从 JSON 数据生成 selector 列表（与 src/steps/selectors.py 的 _generate_selectors_from_json 对齐）

    关键点：
    - dict: .<key> 递归
    - list: 只遍历 list 里的 dict；filter key 优先使用 preferred_filter_keys，否则使用第一个 key
    
    Args:
        json_data: JSON 数据
        preferred_filter_keys: 优先使用的 filter key 列表（如 ["ruleCode", "name", "loss_num"]）
    """
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
            # 使用优先 key 策略：优先使用 preferred_filter_keys，否则使用第一个 key
            for item in node:
                if not isinstance(item, dict):
                    continue
                fk = _preferred_key(item, preferred_filter_keys)
                if fk is None:
                    continue
                fv = item.get(fk)
                filt = f".[<{_escape_angle_value(fk)}>=" f"<{_escape_angle_value(fv)}>]"
                child_selector = f"{selector_prefix}{filt}" if selector_prefix else f"{filt}"
                walk(item, child_selector)
            return

    walk(json_data, selector_prefix="")
    return sorted(set(out))


# =============================================================================
# selector 导航/取值
# =============================================================================

def extract_value_by_selector(data: Any, selector: str) -> Any:
    """
    通过 selector 从 JSON 中提取值
    提取失败返回 None
    """
    try:
        return _navigate_selector(data, selector)
    except Exception as e:
        logger.debug(f"提取值失败 selector={selector} err={e}")
        return None


def _navigate_selector(node: Any, selector: str) -> Any:
    """
    递归导航 selector 路径
    支持：
    - .<key>：普通字段访问
    - .[<key>=<value>]：列表过滤（与 selectors.py 生成规则一致）
    """
    if not selector:
        return node

    if selector.startswith("."):
        selector = selector[1:]

    if not selector:
        return node

    if selector.startswith("["):
        match = re.match(r"^\[<([^>]+)>=<([^>]+)>\](.*)$", selector)
        if not match:
            raise ValueError(f"无效的列表过滤 selector: {selector}")

        filter_key = match.group(1)
        filter_value = match.group(2)
        rest = match.group(3)

        if not isinstance(node, list):
            raise ValueError(f"节点不是列表，无法应用过滤器: {selector}")

        for item in node:
            if isinstance(item, dict) and str(item.get(filter_key)) == filter_value:
                return _navigate_selector(item, rest)

        raise ValueError(f"列表中未找到匹配项: {filter_key}={filter_value}")

    # 普通 dict key 访问
    match = re.match(r"^<([^>]+)>(.*)$", selector)
    if match:
        key = match.group(1)
        rest = match.group(2)
    else:
        next_sep = min(
            (selector.find(".") if selector.find(".") >= 0 else len(selector)),
            (selector.find("[") if selector.find("[") >= 0 else len(selector)),
        )
        key = selector[:next_sep]
        rest = selector[next_sep:]

    if not isinstance(node, dict):
        raise ValueError(f"节点不是字典，无法访问字段: {key}")

    if key not in node:
        raise ValueError(f"字段不存在: {key}")

    return _navigate_selector(node[key], rest)


# =============================================================================
# field_name / rule_code / field_type
# =============================================================================

def extract_rule_code(field: str) -> str:
    """
    从 selector 字符串中提取 rule_code
    格式：[<ruleCode>=<xxx>] 或 [<name>=<xxx>]
    返回 xxx，如果没有则返回空字符串
    """
    match = re.search(r"\[<ruleCode>=<([^>]+)>\]", field)
    if match:
        return match.group(1)

    match = re.search(r"\[<name>=<([^>]+)>\]", field)
    if match:
        return match.group(1)

    return ""


def extract_field_name(field: str) -> str:
    """
    从 selector 字符串中提取 field_name

    规则：
    1. 对于 [<ruleCode>=<metadata_raw>] 的字段：
       - 如果存在 .<camera_info> 或 .<joint_info>，显示其后的所有 <> 中的内容
       - 否则打印 .<metadata> 之后的所有 <> 中的内容
       - 如果不符合上述条件，打印 .[<name>=<metadata.json>] 之后的所有 <> 中的内容
    2. 对于其他字段：
       取最后一个 [<name>=<xxx>] 中的 xxx 和最后一个 .<yyy> 中的 yyy
       格式为 "xxx-yyy"
    """
    rule_code = extract_rule_code(field)

    if rule_code == "metadata_raw":
        if ".<camera_info>" in field or ".<joint_info>" in field:
            if ".<camera_info>" in field:
                start_pos = field.find(".<camera_info>")
                after_text = field[start_pos + len(".<camera_info>") :]
            else:
                start_pos = field.find(".<joint_info>")
                after_text = field[start_pos + len(".<joint_info>") :]

            matches = re.findall(r"<([^>]+)>", after_text)
            return "-".join(matches)

        if ".<metadata>" in field:
            start_pos = field.find(".<metadata>")
            after_text = field[start_pos + len(".<metadata>") :]
            matches = re.findall(r"<([^>]+)>", after_text)
            return "-".join(matches)

        pattern = r"\[<name>=<metadata\.json>\]"
        m = re.search(pattern, field)
        if m:
            after_text = field[m.end() :]
            matches = re.findall(r"<([^>]+)>", after_text)
            return "-".join(matches)

        return ""

    # 非 metadata_raw
    name_matches = re.findall(r"\[<name>=<([^>]+)>\]", field)
    last_name = name_matches[-1] if name_matches else ""

    dot_matches = re.findall(r"\.<([^>]+)>", field)
    last_dot = dot_matches[-1] if dot_matches else ""

    parts: List[str] = []
    if last_name:
        parts.append(last_name)
    if last_dot:
        parts.append(last_dot)

    return "-".join(parts)


def extract_field_type(field: str) -> str:
    """从 selector 字符串中提取 field_type：取最后一个 .<xxx> 中的内容"""
    dot_matches = re.findall(r"\.<([^>]+)>", field)
    return dot_matches[-1] if dot_matches else ""


# =============================================================================
# 数据类型判断
# =============================================================================

def determine_data_type(value: Any, rule_code: str) -> str:
    """
    判断值的数据类型（numeric 或 non_numeric）

    规则：
    1. rulecode 为 metadata_raw → non_numeric
    2. bool 值 → non_numeric
    3. None → non_numeric
    4. list → non_numeric
    5. 空字符串 → non_numeric
    6. 字符串 "null" → numeric
    7. 字符串无法转为数字 → non_numeric
    8. 其他数字类型 → numeric
    """
    if rule_code == "metadata_raw":
        return "non_numeric"

    if isinstance(value, bool):
        return "non_numeric"

    if value is None:
        return "non_numeric"

    if isinstance(value, list):
        return "non_numeric"

    if isinstance(value, str):
        if value.strip() == "":
            return "non_numeric"
        if value.strip().lower() == "null":
            return "numeric"
        try:
            float(value)
            return "numeric"
        except (ValueError, TypeError):
            return "non_numeric"

    if isinstance(value, (int, float)):
        return "numeric"

    if isinstance(value, dict):
        return "non_numeric"

    return "non_numeric"


# =============================================================================
# 过滤器函数（保持原样）
# =============================================================================

_ANGLE_RE = re.compile(r"<([^<>]+)>")


def apply_filters(selectors: List[str], model: str, filter_config: Dict[str, Any]) -> List[str]:
    """
    应用过滤规则到 selector 列表
    """
    if not filter_config.get("enable_filter", False):
        return sorted(set(selectors))

    angle_allowlist = filter_config.get("angle_allowlist", [])
    charseq_allowlist = filter_config.get("charseq_allowlist", [])
    angle_blocklist = filter_config.get("angle_blocklist", [])

    angle_match_mode = filter_config.get("angle_match_mode", "contains")
    charseq_match_mode = filter_config.get("charseq_match_mode", "both")
    charseq_case_sensitive = filter_config.get("charseq_case_sensitive", True)

    enable_angle_allow_add = filter_config.get("enable_angle_allow_add", True)
    enable_charseq_allow_add = filter_config.get("enable_charseq_allow_add", True)
    enable_angle_block_exclude = filter_config.get("enable_angle_block_exclude", True)
    allow_all_if_no_allowlists = filter_config.get("allow_all_if_no_allowlists", True)

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
        else:  # both
            if all(p in s for p in parts):
                return True

    return False


def _match_blocklist_rules(selector: str, rules: List[Any], model: str) -> bool:
    """
    匹配黑名单规则
    规则格式：
    - models: ["MODEL1", "MODEL2"]  # 可选
      contains: ["str1", "str2"]    # 必选
    """
    if not rules:
        return False

    for rule in rules:
        if not isinstance(rule, dict):
            continue

        rule_models = rule.get("models", [])
        if rule_models and isinstance(rule_models, list):
            rule_models_lower = [str(m).lower() for m in rule_models]
            if model.lower() not in rule_models_lower:
                continue

        contains = rule.get("contains", [])
        if not contains or not isinstance(contains, list):
            continue

        if all(str(c) in selector for c in contains):
            return True

    return False


# =============================================================================
# value 序列化
# =============================================================================

def value_to_string(value: Any) -> Optional[str]:
    """
    将值转换为字符串用于存储
    - None -> None（数据库存 NULL）
    - dict/list -> json
    - 其他 -> str
    """
    if value is None:
        return None

    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    return str(value)
