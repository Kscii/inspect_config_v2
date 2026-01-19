# src/run.py
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.config_loader import load_config_with_local, normalize_and_validate_config
from core.logger import build_logger
from core.pipeline import run_pipeline


def _repo_root_from_here() -> Path:
    return Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="statistic_inspect pipeline runner")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径（默认 repo_root/config.yaml）")
    parser.add_argument("--debug", action="store_true", help="强制使用 debug 日志模式（覆盖 global.log_mode）")
    parser.add_argument("--dry-run", action="store_true", help="全局 DRY_RUN（覆盖 global.dry_run 与各 step）")
    args = parser.parse_args(argv)

    repo_root = _repo_root_from_here()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = repo_root / config_path

    try:
        raw_cfg = load_config_with_local(config_path)
        cfg = normalize_and_validate_config(raw_cfg)
    except Exception as e:
        print(f"[FATAL] 配置加载/校验失败：{e}")
        return 2

    if args.debug:
        cfg["global"]["log_mode"] = "debug"
    if args.dry_run:
        cfg["global"]["dry_run"] = True

    logger = build_logger(
        repo_root=repo_root,
        log_dirname=cfg["global"].get("log_dirname", "log"),
        log_mode=cfg["global"].get("log_mode", "normal"),
        console_level=cfg["global"].get("console_level", None),
    )

    try:
        run_pipeline(repo_root=repo_root, cfg=cfg, logger=logger)
        logger.info("[DONE] pipeline finished successfully")
        return 0
    except Exception as e:
        logger.exception("[FATAL] pipeline failed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
