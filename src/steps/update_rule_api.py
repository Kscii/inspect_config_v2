# src/steps/update_rule_api.py
# -*- coding: utf-8 -*-
"""
update_rule_api step
职责：
1) 从 config.yaml 读取 current_preset -> base_url
2) 读取 access_token
3) 对 base 和 full 两套分别发送 PUT 请求
   - base: /data-collector/rule/update       配置来自 {model}_ranges.txt
   - full: /data-collector/rule/update_full  配置来自 {model}_ranges_full.txt
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class UpdateApiResult:
    updated_models: List[str]
    updated_models_full: List[str]


_ONE_LINE_WS = re.compile(r"\s+")


def _one_line(s: Any) -> str:
    """把任意字符串压成单行（去掉换行/回车/制表符，并折叠空白）。"""
    if s is None:
        return ""
    if not isinstance(s, str):
        try:
            s = str(s)
        except Exception:
            return ""
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = _ONE_LINE_WS.sub(" ", s).strip()
    return s


def _full_json_one_line(resp_text: Optional[str]) -> str:
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
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return _one_line(resp_text)


def _parse_api_fields(resp_text: Optional[str]) -> Tuple[Optional[bool], Optional[str], Optional[Any]]:
    if resp_text is None:
        return None, None, None
    t = resp_text.strip()
    if not t:
        return None, None, None
    try:
        obj = json.loads(t)
    except Exception:
        return None, None, None

    success = obj.get("success", None)
    msg = obj.get("msg", None)
    data = obj.get("data", None)

    if isinstance(success, str):
        ss = success.strip().lower()
        if ss == "true":
            success = True
        elif ss == "false":
            success = False
        else:
            success = None

    if msg is not None and not isinstance(msg, str):
        msg = _one_line(msg) or None

    return success, msg, data


def _build_model_to_id_map(global_cfg: Dict[str, Any], preset: str) -> Dict[str, int]:
    """
    从 global.rule_id_to_model_presets.<preset> 读取:
      { <id(int/str)>: "<MODEL_NAME>" }
    反转成:
      { "<MODEL_NAME>": <id int> }
    """
    gmap_all = (global_cfg or {}).get("rule_id_to_model_presets", None)
    if not isinstance(gmap_all, dict):
        raise KeyError("[update_rule_api] global.rule_id_to_model_presets 缺失或不是 dict")

    id_to_model = gmap_all.get(preset, None)
    if not isinstance(id_to_model, dict):
        raise KeyError(f"[update_rule_api] global.rule_id_to_model_presets.{preset} 缺失或不是 dict")

    model_to_id: Dict[str, int] = {}
    for rid, name in id_to_model.items():
        if name is None:
            continue
        n = str(name).strip()
        if not n:
            continue
        try:
            rid_i = int(rid)
        except Exception:
            # rid 不是 int：跳过（配置错误）
            continue
        # 注意：如果同一个 model 出现多次，后写会覆盖前写（建议你在 YAML 保证唯一性）
        model_to_id[n] = rid_i

    return model_to_id


def run_step(
    repo_root: Path,
    global_cfg: Dict[str, Any],
    step_cfg: Dict[str, Any],
    runtime: Dict[str, Any],
) -> UpdateApiResult:
    logger = runtime.get("logger")
    log_mode = runtime.get("log_mode", global_cfg.get("log_mode", "normal"))

    enable_full: bool = bool(step_cfg.get("enable_full", True))

    models: List[str] = runtime["models"]

    presets = global_cfg["presets"]
    current_preset: str = str(step_cfg.get("current_preset", "dev"))
    base_url: str = str(presets[current_preset]["base_url"]).rstrip("/")

    # 从 global 读映射（dev/prod 各一套）
    model_to_rule_id = _build_model_to_id_map(global_cfg, current_preset)

    put_path: str = str(step_cfg.get("put_path", "/data-collector/rule/update"))
    put_path_full: str = str(step_cfg.get("put_path_full", "/data-collector/rule/update_full"))

    access_token: str = str(step_cfg.get("access_token", "")).strip()

    dry_run: bool = bool(step_cfg.get("dry_run", False)) or bool(
        runtime.get("dry_run", global_cfg.get("dry_run", False))
    )
    verify_tls: bool = bool(step_cfg.get("verify_tls", True))
    timeout_sec: int = int(step_cfg.get("timeout_sec", 30))

    csv_output_dirname: str = str(step_cfg.get("csv_output_dirname", "csv_output"))

    # 缺少 rule_id / 缺少 txt 的失败策略
    fail_if_id_missing: bool = bool(step_cfg.get("fail_if_id_missing", True))
    fail_if_txt_missing: bool = bool(step_cfg.get("fail_if_txt_missing", fail_if_id_missing))

    if not access_token:
        raise ValueError("[update_rule_api] access_token 为空（你要求写在配置文件中）")

    url_base = f"{base_url}{put_path}"
    url_full = f"{base_url}{put_path_full}"

    updated_base: List[str] = []
    updated_full: List[str] = []

    # 先 base
    updated_base = _run_one_update(
        variant="base",
        url=url_base,
        repo_root=repo_root,
        logger=logger,
        log_mode=log_mode,
        models=models,
        model_to_rule_id=model_to_rule_id,
        access_token=access_token,
        dry_run=dry_run,
        verify_tls=verify_tls,
        timeout_sec=timeout_sec,
        csv_output_dirname=csv_output_dirname,
        txt_name_tpl="{model}_ranges.txt",
        fail_if_id_missing=fail_if_id_missing,
        fail_if_txt_missing=fail_if_txt_missing,
    )

    # 再 full（S1：任一失败即终止）
    if enable_full:
        updated_full = _run_one_update(
            variant="full",
            url=url_full,
            repo_root=repo_root,
            logger=logger,
            log_mode=log_mode,
            models=models,
            model_to_rule_id=model_to_rule_id,
            access_token=access_token,
            dry_run=dry_run,
            verify_tls=verify_tls,
            timeout_sec=timeout_sec,
            csv_output_dirname=csv_output_dirname,
            txt_name_tpl="{model}_ranges_full.txt",
            fail_if_id_missing=fail_if_id_missing,
            fail_if_txt_missing=fail_if_txt_missing,
        )

    return UpdateApiResult(updated_models=updated_base, updated_models_full=updated_full)


def _run_one_update(
    variant: str,
    url: str,
    repo_root: Path,
    logger,
    log_mode: str,
    models: List[str],
    model_to_rule_id: Dict[str, int],
    access_token: str,
    dry_run: bool,
    verify_tls: bool,
    timeout_sec: int,
    csv_output_dirname: str,
    txt_name_tpl: str,
    fail_if_id_missing: bool,
    fail_if_txt_missing: bool,
) -> List[str]:
    updated: List[str] = []

    headers = {
        "Content-Type": "application/json",
        "accesstoken": access_token,
    }

    for model in models:
        rule_id = model_to_rule_id.get(model)

        if rule_id is None:
            msg = f"[update_rule_api:{variant}] model={model} 缺少 rule_id 映射（preset map）"
            if fail_if_id_missing:
                raise KeyError(msg)
            _log(logger, log_mode, msg)
            continue

        txt_path = repo_root / csv_output_dirname / model / txt_name_tpl.format(model=model)
        if not txt_path.exists():
            msg = f"[update_rule_api:{variant}] model={model} 缺少 ranges.txt: {txt_path}"
            if fail_if_txt_missing:
                raise FileNotFoundError(msg)
            _log(logger, log_mode, msg)
            continue

        # 注意：pack_csv_txt 输出是 literal 单字符串（包含 \\n），这里必须原样作为 config 发送
        config_text = txt_path.read_text(encoding="utf-8", errors="strict")
        body_obj = {"id": int(rule_id), "config": config_text}
        body_bytes = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")

        if dry_run:
            _log(
                logger,
                log_mode,
                f"[update_rule_api:{variant}] DRY_RUN PUT {url} model={model} id={rule_id} bytes={len(body_bytes)}",
            )
            updated.append(model)
            continue

        resp_code, resp_text = _http_put(
            url, body_bytes, headers=headers, timeout=timeout_sec, verify_tls=verify_tls
        )

        resp_one_line = _full_json_one_line(resp_text)
        success, msg, _data = _parse_api_fields(resp_text)

        # 业务判定：success=false 直接报错（即使 HTTP=200），报错内容就是 msg
        if success is False:
            raise RuntimeError(f"[update_rule_api:{variant}] model={model} id={rule_id} success=false msg={msg}")

        if 200 <= resp_code < 300:
            _log(
                logger,
                log_mode,
                f"[update_rule_api:{variant}] OK model={model} id={rule_id} code={resp_code} resp={resp_one_line}",
            )
            updated.append(model)
        else:
            raise RuntimeError(
                f"[update_rule_api:{variant}] FAILED model={model} id={rule_id} code={resp_code} resp={resp_one_line}"
            )

    return updated


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

    兼容：即便 HTTP 非 2xx，也尽量读出 body 文本（便于日志/错误信息）。
    """
    req = urllib.request.Request(url=url, data=data, headers=headers, method="PUT")

    # NOTE: verify_tls=False 的完整关闭需要 SSLContext，这里保持最小依赖不实现。
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = int(resp.status)
            text = resp.read().decode("utf-8", errors="ignore")
            return code, text
    except urllib.error.HTTPError as e:
        # HTTPError 也有 response body
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        return int(getattr(e, "code", 0) or 0), body
    except Exception as e:
        # 网络/连接错误：用 code=0 表示
        return 0, _one_line(str(e))


def _log(logger, log_mode: str, msg: str) -> None:
    if logger is None:
        print(msg)
        return
    logger.info(msg)
