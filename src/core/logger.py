# src/core/logger.py
# -*- coding: utf-8 -*-
"""
统一日志
要求：
- 两种模式：
  - normal：尽量接近你旧 log 输出（简洁）
  - debug：包含更多细节（文件名、行号、线程、时间等）
- 不输出 pipeline_report.json（你已要求删除）
- 落盘到 repo_root/log/ 下
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional


def build_logger(
    repo_root: Path,
    log_dirname: str = "log",
    log_mode: str = "normal",
    console_level: Optional[str] = None,
) -> logging.Logger:
    log_dir = repo_root / log_dirname
    log_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"pipeline_{ts}_{log_mode}.log"

    logger = logging.getLogger("pipeline")
    logger.setLevel(logging.DEBUG)  # logger 本身开到 DEBUG，具体由 handler 控制
    logger.handlers.clear()
    logger.propagate = False

    # ---------- 文件日志 ----------
    fh = logging.FileHandler(log_path, encoding="utf-8")
    if log_mode == "debug":
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(_debug_formatter())
    else:
        fh.setLevel(logging.INFO)
        fh.setFormatter(_normal_formatter())
    logger.addHandler(fh)

    # ---------- 控制台日志 ----------
    ch = logging.StreamHandler()
    if console_level:
        level = getattr(logging, console_level, logging.INFO)
    else:
        level = logging.DEBUG if log_mode == "debug" else logging.INFO
    ch.setLevel(level)
    ch.setFormatter(_debug_formatter() if log_mode == "debug" else _normal_formatter())
    logger.addHandler(ch)

    logger.info("[logger] log_mode=%s log_path=%s", log_mode, log_path)
    return logger


def _normal_formatter() -> logging.Formatter:
    # 简洁：时间 + 等级 + 信息
    return logging.Formatter(fmt="%(asctime)s - %(levelname)s - %(message)s")


def _debug_formatter() -> logging.Formatter:
    # 详细：时间 + 等级 + 文件:行号 + 函数 + 信息
    return logging.Formatter(
        fmt="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(funcName)s - %(message)s"
    )
