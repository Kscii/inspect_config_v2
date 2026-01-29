# src/core/step_registry.py
# -*- coding: utf-8 -*-
"""
Step 注册与加载
- 固定 step 顺序（不可变）：STEP_ORDER
- 为了避免“必须把项目安装成包/必须 import steps.xxx”，这里按文件路径加载 step
- 约定：每个 step 文件必须提供 run_step(...) 函数
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List

# 固定顺序（与你重构前一致，不要改顺序）
STEP_ORDER: List[str] = [
    "download",
    "selectors",
    "collect",
    "find_range",
    "find_range_full",
    "test_range",
    "pack_csv",
    "build_sql",
    "update_rule_api",
]


# step_name -> 实际文件名（解决你现在 pack_csv_txt.py 的历史命名）
# 建议：最终把 pack_csv_txt.py 改名为 pack_csv.py，这样这里也可以删掉映射。
STEP_FILE_ALIAS: Dict[str, str] = {
    "pack_csv": "pack_csv_txt.py",
}


def _load_module_from_path(py_path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, str(py_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 step 生成 module spec：{py_path}")

    mod = importlib.util.module_from_spec(spec)

    sys.modules[module_name] = mod

    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def get_step_callable(repo_root: Path, step_name: str) -> Callable[..., Any]:
    """
    返回 step 的可调用函数：run_step(...)
    """
    if step_name not in STEP_ORDER:
        raise ValueError(f"未知 step：{step_name}，允许值={STEP_ORDER}")

    filename = STEP_FILE_ALIAS.get(step_name, f"{step_name}.py")
    step_file = repo_root / "src" / "steps" / filename

    if not step_file.exists():
        raise FileNotFoundError(
            f"找不到 step 文件：{step_file}\n"
            f"请创建：src/steps/{filename}，并实现 run_step(...)"
        )

    mod_name = f"steps_{step_name}"
    mod = _load_module_from_path(step_file, mod_name)

    fn = getattr(mod, "run_step", None)
    if not callable(fn):
        raise AttributeError(
            f"step 模块缺少可调用函数 run_step：{step_file}\n"
            f"请在该文件中实现：def run_step(repo_root, global_cfg, step_cfg, runtime): ..."
        )

    return fn
