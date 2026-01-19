# src/steps/update_rule_api.py
# -*- coding: utf-8 -*-
"""
update_rule_api step
职责：
1) 从 config.yaml 读取 current_preset -> base_url
2) 读取 access_token（你要求必须写在配置文件里，因每 2 小时更新一次）
3) 逐 model 发送 PUT 请求更新规则
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class UpdateApiResult:
    updated_models: List[str]


def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> UpdateApiResult:
    logger = runtime.get("logger")
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode", "normal"))

    models: List[str] = runtime["models"]
    model_to_ranges_txt: Dict[str, Path] = runtime["model_to_ranges_txt"]

    presets = global_cfg["presets"]
    current_preset: str = str(step_cfg.get("current_preset", "dev"))
    base_url: str = str(presets[current_preset]["base_url"]).rstrip("/")

    put_path: str = str(step_cfg.get("put_path", "/data-collector/rule/update"))
    access_token: str = str(step_cfg.get("access_token", "")).strip()

    dry_run: bool = bool(step_cfg.get("dry_run", False)) or bool(runtime.get("dry_run", global_cfg.get("dry_run", False)))
    verify_tls: bool = bool(step_cfg.get("verify_tls", True))
    timeout_sec: int = int(step_cfg.get("timeout_sec", 30))

    csv_output_dirname: str = str(step_cfg.get("csv_output_dirname", "csv_output"))
    fail_if_id_missing: bool = bool(step_cfg.get("fail_if_id_missing", True))

    if not access_token:
        raise ValueError("[update_rule_api] access_token 为空（你要求写在配置文件中）")

    url = f"{base_url}{put_path}"

    updated: List[str] = []

    for model in models:
        txt_path = model_to_ranges_txt.get(model) or (repo_root / csv_output_dirname / model / f"{model}_ranges.txt")
        if not txt_path.exists():
            msg = f"[update_rule_api] model={model} 缺少 ranges.txt: {txt_path}"
            if fail_if_id_missing:
                raise FileNotFoundError(msg)
            _log(logger, log_mode, msg)
            continue

        payload_text = txt_path.read_text(encoding="utf-8")
        # 发送 JSON：这里直接把 pack_csv 输出的 payload 作为 body
        # 如果你们接口需要额外字段（比如 ruleConfigId），后续可在这里扩展：
        # body = {"model": model, "rules": ..., "id": ...}
        try:
            body_obj = json.loads(payload_text)
        except Exception:
            # 万一 txt 不是 JSON（比如你以前的格式），也兼容：当作字符串塞进去
            body_obj = {"model": model, "payload": payload_text}

        body_bytes = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            # 按你接口要求：header 名叫 accesstoken
            "accesstoken": access_token,
        }

        if dry_run:
            _log(logger, log_mode, f"[update_rule_api] DRY_RUN PUT {url} model={model} bytes={len(body_bytes)}")
            updated.append(model)
            continue

        resp_code, resp_text = _http_put(url, body_bytes, headers=headers, timeout=timeout_sec, verify_tls=verify_tls)
        if 200 <= resp_code < 300:
            _log(logger, log_mode, f"[update_rule_api] OK model={model} code={resp_code}")
            updated.append(model)
        else:
            # 高优先级错误：直接中断，符合你的“及时上报并中断流程”
            raise RuntimeError(f"[update_rule_api] FAILED model={model} code={resp_code} resp={resp_text[:500]}")

    return UpdateApiResult(updated_models=updated)


def _http_put(
    url: str,
    data: bytes,
    headers: Dict[str, str],
    timeout: int,
    verify_tls: bool,
) -> Tuple[int, str]:
    """
    使用标准库发 PUT，避免引入额外依赖。
    verify_tls=False 时：这里不做复杂 SSL 配置（建议生产保持 True）
    """
    req = urllib.request.Request(url=url, data=data, headers=headers, method="PUT")
    # 说明：verify_tls 的完整关闭通常需要自定义 SSLContext；
    #       为了保持依赖最小，这里默认不关（verify_tls 参数保留以匹配配置项）
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        code = int(resp.status)
        text = resp.read().decode("utf-8", errors="ignore")
        return code, text


def _log(logger, log_mode: str, msg: str) -> None:
    if logger is None:
        print(msg)
        return
    logger.info(msg)
