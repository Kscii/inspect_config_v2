# src/core/config_loader.py
# -*- coding: utf-8 -*-
"""
配置加载与校验
- 支持 config.local.yaml 覆盖 config.yaml（优先级更高）
- 深度合并（dict 递归合并；list 视为整体覆盖）
- 兼容 presets 两种写法：
  1) 推荐：global.presets（你当前的 config.yaml 就是这种）
  2) 兼容：顶层 presets（旧写法）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml


def _read_yaml_map(path: Path, *, required: bool) -> Dict[str, Any]:
    """
    读取 YAML，并保证返回 dict（YAML map）
    """
    if not path.exists():
        if required:
            raise FileNotFoundError(f"找不到配置文件：{path}")
        return {}

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError(f"配置文件顶层必须是 YAML map（dict）：{path}")

    return data


def _deep_merge(base: Any, override: Any) -> Any:
    """
    深度合并：override 覆盖 base
    - dict: 递归合并（逐条 key）
    - list/tuple: 视为“整体覆盖”（override 直接替换 base）
    - 其他类型: override 直接替换
    """
    if override is None:
        # 说明：如果 local 显式写 null，我们认为就是要覆盖为 None
        return None

    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for k, v in override.items():
            if k in out:
                out[k] = _deep_merge(out[k], v)
            else:
                out[k] = v
        return out

    if isinstance(override, (list, tuple)):
        return list(override)

    return override


def load_config_with_local(config_path: Path) -> Dict[str, Any]:
    """
    加载配置：
    - base: config_path（通常是 repo_root/config.yaml）
    - local: 与 base 同目录下的 config.local.yaml（可选）
    返回：合并后的 raw dict（未做结构补全/校验）
    """
    config_path = config_path.resolve()
    repo_dir = config_path.parent
    local_path = repo_dir / "config.local.yaml"

    base = _read_yaml_map(config_path, required=True)
    local = _read_yaml_map(local_path, required=False)

    merged = _deep_merge(base, local)
    if not isinstance(merged, dict):
        raise ValueError("合并后的配置不是 dict（异常情况）")
    return merged


def _extract_presets(cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    返回：(global_cfg, presets)
    - 优先使用 global.presets（你当前配置）
    - 兼容旧的顶层 presets
    """
    global_cfg = cfg.get("global") or {}
    if not isinstance(global_cfg, dict):
        raise ValueError("顶层 global 必须是一个 map/dict")

    presets = global_cfg.get("presets")
    if presets is None:
        presets = cfg.get("presets")  # 兼容旧写法

    if presets is None:
        raise ValueError("缺少 presets：请在 global.presets 下提供 dev/prod（包含 base_url 与 obs_bucket）")

    if not isinstance(presets, dict):
        raise ValueError("presets 必须是一个 map/dict")

    return global_cfg, presets


def _validate_presets(presets: Dict[str, Any]) -> None:
    """
    校验 presets 必须包含 dev/prod，且都有 base_url 与 obs_bucket
    """
    for name in ("dev", "prod"):
        p = presets.get(name)
        if not isinstance(p, dict):
            raise ValueError(f"缺少 presets.{name}（必须包含 base_url 与 obs_bucket）")
        if not p.get("base_url"):
            raise ValueError(f"缺少 presets.{name}.base_url")
        if not p.get("obs_bucket"):
            raise ValueError(f"缺少 presets.{name}.obs_bucket")


def normalize_and_validate_config(raw_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    做最基础的结构校验与轻量 normalize：
    - 校验 global/presets 存在且合法
    - 不强行重排你的结构；尽量保持“config 的原样结构”
    """
    if not isinstance(raw_cfg, dict):
        raise ValueError("配置加载结果不是 dict")

    global_cfg, presets = _extract_presets(raw_cfg)
    _validate_presets(presets)

    # 额外：确保 global.steps_to_run 存在（你当前配置有）
    steps_to_run = global_cfg.get("steps_to_run")
    if steps_to_run is None or not isinstance(steps_to_run, list) or not steps_to_run:
        raise ValueError("缺少 global.steps_to_run 或格式不正确（必须是非空 list）")

    # 额外：确保每个 step 配置段存在（可选：你想严格就启用）
    # 这里不强制每个 step 都要有段，避免未来新增 step 还没写配置就直接炸
    # 但你要“稳定性+及时中断”，可以在 runner 层对 steps_to_run 做更严格校验。

    return raw_cfg
