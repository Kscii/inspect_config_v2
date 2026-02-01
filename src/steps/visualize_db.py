# src/steps/visualize_db.py
# -*- coding: utf-8 -*-
"""
visualize_db step
职责：
1) 启动 Plotly Dash 服务器，可视化数据库中的检测数据
2) 提供分层导航树（Model → Rule Code → Field）
3) 支持时间范围过滤、多字段对比、统计分析、PNG 导出
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

# 使用绝对导入
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from visualize_db_app import app


@dataclass
class VisualizeDbResult:
    message: str


def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> VisualizeDbResult:
    logger = runtime.get("logger")
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode", "normal"))

    # 读取配置
    host = step_cfg.get("host", "127.0.0.1")
    port = step_cfg.get("port", 8050)
    debug = step_cfg.get("debug", False)

    # 数据库配置传递给 app
    db_config = {
        "host": step_cfg.get("db_host"),
        "port": step_cfg.get("db_port"),
        "database": step_cfg.get("db_database"),
        "user": step_cfg.get("db_user"),
        "password": step_cfg.get("db_password"),
    }

    _log(logger, log_mode, f"[visualize_db] 启动 Dash 服务器: http://{host}:{port}")
    _log(logger, log_mode, f"[visualize_db] 数据库: {db_config['database']}@{db_config['host']}")

    # 初始化 app 的数据库配置
    app.server.config["DB_CONFIG"] = db_config

    # 启动服务器
    app.run(host=host, port=port, debug=debug)

    return VisualizeDbResult(message="Dash server started successfully")


def _log(logger, log_mode: str, msg: str) -> None:
    if logger is None:
        print(msg)
        return
    logger.info(msg)
