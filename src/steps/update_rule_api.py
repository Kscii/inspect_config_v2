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
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple


@dataclass
class UpdateApiResult:
    updated_models: List[str]


_ONE_LINE_WS = re.compile(r"\s+")


def _one_line(s: str) -> str:
    """把任意字符串压成单行（去掉换行/回车/制表符，并折叠空白）。"""
    if s is None:
        return ""
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = _ONE_LINE_WS.sub(" ", s).strip()
    return s


def _full_json_one_line(resp_text: str) -> str:
    """
    尝试把返回体解析为 JSON，并用紧凑 JSON（单行）完整输出。
    - 若不是 JSON，则退化为原文本压单行。
    """
    if resp_text is None:
        return ""
    t = resp_text.strip()
    if not t:
        return ""
    try:
        obj = json.loads(t)
        # 紧凑单行 JSON：完整但不换行
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return _one_line(resp_text)


def _parse_success_msg(resp_text: str) -> Tuple[bool | None, str | None]:
    """
    解析接口标准响应：{"success": true/false, "msg": "...", ...}
    返回 (success, msg)
    - success: True / False / None（无法解析或缺字段）
    - msg: str 或 None
    """
    if resp_text is None:
        return None, None
    t = resp_text.strip()
    if not t:
        return None, None
    try:
        obj = json.loads(t)
    except Exception:
        return None, None

    success = obj.get("success", None)
    msg = obj.get("msg", None)

    # success 可能是字符串
    if isinstance(success, str):
        ss = success.strip().lower()
        if ss == "true":
            success = True
        elif ss == "false":
            success = False
        else:
            success = None

    if msg is not None and not isinstance(msg, str):
        try:
            msg = str(msg)
        except Exception:
            msg = None

    return success, msg


def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> UpdateApiResult:
    logger = runtime.get("logger")
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode", "normal"))

    models: List[str] = runtime["models"]
    # 允许单步运行：没有上游 pack_csv 时，映射可能不存在
    model_to_ranges_txt: Dict[str, Path] = runtime.get("model_to_ranges_txt", {}) or {}

    presets = global_cfg["presets"]
    current_preset: str = str(step_cfg.get("current_preset", "dev"))
    base_url: str = str(presets[current_preset]["base_url"]).rstrip("/")

    put_path: str = str(step_cfg.get("put_path", "/data-collector/rule/update"))
    access_token: str = str(step_cfg.get("access_token", "")).strip()

    dry_run: bool = bool(step_cfg.get("dry_run", False)) or bool(
        runtime.get("dry_run", global_cfg.get("dry_run", False))
    )
    verify_tls: bool = bool(step_cfg.get("verify_tls", True))
    timeout_sec: int = int(step_cfg.get("timeout_sec", 30))

    csv_output_dirname: str = str(step_cfg.get("csv_output_dirname", "csv_output"))
    fail_if_id_missing: bool = bool(step_cfg.get("fail_if_id_missing", True))

    if not access_token:
        raise ValueError("[update_rule_api] access_token 为空（你要求写在配置文件中）")

    url = f"{base_url}{put_path}"

    updated: List[str] = []

    for model in models:
        # 兜底：若没有 runtime 映射，则按固定产物路径寻找
        txt_path = model_to_ranges_txt.get(model) or (
            repo_root / csv_output_dirname / model / f"{model}_ranges.txt"
        )

        if not txt_path.exists():
            msg = f"[update_rule_api] model={model} 缺少 ranges.txt: {txt_path}"
            if fail_if_id_missing:
                raise FileNotFoundError(msg)
            _log(logger, log_mode, msg)
            continue

        payload_text = txt_path.read_text(encoding="utf-8")

        # pack_csv_txt 输出通常是 literal 字符串（带 \n），可能不是 JSON
        # 这里保持原逻辑：能 parse JSON 就直接用；否则包一层
        try:
            body_obj = json.loads(payload_text)
        except Exception:
            body_obj = {"model": model, "payload": payload_text}

        body_bytes = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            # 按你接口要求：header 名叫 accesstoken（但不要打印它）
            "accesstoken": access_token,
        }

        if dry_run:
            _log(
                logger,
                log_mode,
                f"[update_rule_api] DRY_RUN PUT {url} model={model} bytes={len(body_bytes)}",
            )
            updated.append(model)
            continue

        resp_code, resp_text = _http_put(
            url, body_bytes, headers=headers, timeout=timeout_sec, verify_tls=verify_tls
        )

        # ✅ 日志：完整返回体（单行）
        resp_one_line = _full_json_one_line(resp_text)

        # ✅ 业务判定：success=false 直接报错（即使 HTTP=200）
        success, msg = _parse_success_msg(resp_text)
        if success is False:
            # 报错内容就是 msg（按你的要求）
            raise RuntimeError(str(msg) if msg else "success=false")

        # ✅ HTTP 判定保留：非 2xx 仍然报错
        if 200 <= resp_code < 300:
            _log(
                logger,
                log_mode,
                f"[update_rule_api] OK model={model} code={resp_code} resp={resp_one_line}",
            )
            updated.append(model)
        else:
            raise RuntimeError(
                f"[update_rule_api] FAILED model={model} code={resp_code} resp={resp_one_line}"
            )

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
