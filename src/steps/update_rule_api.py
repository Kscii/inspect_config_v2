# src/steps/update_rule_api.py
# -*- coding: utf-8 -*-
"""
update_rule_api step
职责：
1) 从 config.yaml 读取 current_preset -> base_url
2) 根据 preset.region 读取对应 access token（shanghai_access_token / zhengzhou_access_token）
3) 对 base 和 full 两套分别发送 PUT 请求（同一路径，不同 body 字段）
   - endpoint: /data-collector/rule/update
   - base: body = {"id": <rule_id>, "config": "<literal txt>"}          txt来自 {model}_ranges.txt
   - full: body = {"id": <rule_id>, "configFull": "<literal txt>"}      txt来自 {model}_ranges_full.txt
4) 额外更新 csvPath / csvFullPath（读取 {model}_last_path.txt）
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
    """
    尝试解析后端通用返回：
      {"success": true/false, "msg": "...", "data": ...}
    解析失败返回 (None, None, None)
    """
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
            continue
        model_to_id[n] = rid_i

    return model_to_id


def _resolve_access_token(step_cfg: Dict[str, Any], preset_region: str) -> str:
    """
    根据 region 选择 token：
      region=shanghai  -> step_cfg.shanghai_access_token
      region=zhengzhou -> step_cfg.zhengzhou_access_token
    """
    region = (preset_region or "").strip().lower()
    key = f"{region}_access_token"
    token = str(step_cfg.get(key, "")).strip()
    if not token:
        raise ValueError(f"[update_rule_api] 缺少 token：请在 update_rule_api.{key} 填写 accesstoken")
    return token


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
    current_preset: str = str(step_cfg.get("current_preset", "shanghai_dev"))

    if current_preset not in presets:
        raise KeyError(f"[update_rule_api] preset not found: {current_preset}")

    preset = presets[current_preset] or {}
    base_url: str = str(preset["base_url"]).rstrip("/")
    region: str = str(preset.get("region", "")).strip().lower()
    if not region:
        raise ValueError(f"[update_rule_api] preset missing region: {current_preset}")

    # 从 global 读映射（按 preset 名区分）
    model_to_rule_id = _build_model_to_id_map(global_cfg, current_preset)

    put_path: str = str(step_cfg.get("put_path", "/data-collector/rule/update"))
    access_token: str = _resolve_access_token(step_cfg, preset_region=region)

    dry_run: bool = bool(step_cfg.get("dry_run", False)) or bool(
        runtime.get("dry_run", global_cfg.get("dry_run", False))
    )
    verify_tls: bool = bool(step_cfg.get("verify_tls", True))
    timeout_sec: int = int(step_cfg.get("timeout_sec", 30))

    csv_output_dirname: str = str(step_cfg.get("csv_output_dirname", "csv_output"))

    fail_if_id_missing: bool = bool(step_cfg.get("fail_if_id_missing", True))
    fail_if_txt_missing: bool = bool(step_cfg.get("fail_if_txt_missing", fail_if_id_missing))

    url = f"{base_url}{put_path}"
    _log(logger, log_mode, f"[update_rule_api] preset={current_preset} region={region} url={url} enable_full={enable_full}")

    # base
    updated_base = _run_one_update(
        variant="base",
        url=url,
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
        payload_config_key="config",
        fail_if_id_missing=fail_if_id_missing,
        fail_if_txt_missing=fail_if_txt_missing,
    )

    # full（任一失败即终止）
    updated_full: List[str] = []
    if enable_full:
        updated_full = _run_one_update(
            variant="full",
            url=url,
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
            payload_config_key="configFull",
            fail_if_id_missing=fail_if_id_missing,
            fail_if_txt_missing=fail_if_txt_missing,
        )

    # 发送 csvPath 和 csvFullPath
    _run_path_update(
        url=url,
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
        enable_full=enable_full,
        fail_if_id_missing=fail_if_id_missing,
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
    payload_config_key: str,
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

        # pack_csv 输出是 literal 单字符串（可能包含 \\n），这里必须原样作为 config 发送
        config_text = txt_path.read_text(encoding="utf-8", errors="strict")

        body_obj = {"id": int(rule_id), payload_config_key: config_text}
        body_bytes = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")

        if dry_run:
            _log(
                logger,
                log_mode,
                f"[update_rule_api:{variant}] DRY_RUN PUT {url} model={model} id={rule_id} key={payload_config_key} bytes={len(body_bytes)}",
            )
            updated.append(model)
            continue

        resp_code, resp_text = _http_put(
            url, body_bytes, headers=headers, timeout=timeout_sec, verify_tls=verify_tls
        )

        resp_one_line = _full_json_one_line(resp_text)
        success, msg, _data = _parse_api_fields(resp_text)

        # 业务判定：success=false 直接报错（即使 HTTP=200）
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
    _ = verify_tls

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = int(resp.status)
            text = resp.read().decode("utf-8", errors="ignore")
            return code, text
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        return int(getattr(e, "code", 0) or 0), body
    except Exception as e:
        return 0, _one_line(str(e))


def _log(logger, log_mode: str, msg: str) -> None:
    _ = log_mode
    if logger is None:
        print(msg)
        return
    logger.info(msg)


def _read_last_path(repo_root: Path, csv_output_dirname: str, model: str) -> Tuple[Optional[str], Optional[str]]:
    """
    读取 {model}_last_path.txt 文件
    返回 (base_path, full_path)，如果文件不存在或格式错误则返回 (None, None)
    """
    last_path_file = repo_root / csv_output_dirname / model / f"{model}_last_path.txt"
    if not last_path_file.exists():
        return None, None

    try:
        content = last_path_file.read_text(encoding="utf-8").strip()
        lines = [line.strip() for line in content.split("\n") if line.strip()]

        base_path = lines[0] if len(lines) >= 1 else None
        full_path = lines[1] if len(lines) >= 2 else None

        return base_path, full_path
    except Exception:
        return None, None


def _run_path_update(
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
    enable_full: bool,
    fail_if_id_missing: bool,
) -> None:
    """
    读取 {model}_last_path.txt 并发送两次 PUT 请求：
    1) {"id": xxx, "csvPath": "..."}
    2) {"id": xxx, "csvFullPath": "..."}  (仅当 enable_full=True)
    """
    headers = {
        "Content-Type": "application/json",
        "accesstoken": access_token,
    }

    for model in models:
        rule_id = model_to_rule_id.get(model)
        if rule_id is None:
            msg = f"[update_rule_api:path] model={model} 缺少 rule_id 映射"
            if fail_if_id_missing:
                raise KeyError(msg)
            _log(logger, log_mode, msg)
            continue

        base_path, full_path = _read_last_path(repo_root, csv_output_dirname, model)

        if base_path is None:
            _log(logger, log_mode, f"[update_rule_api:path] model={model} 缺少 last_path.txt，跳过 csvPath 更新")
            continue

        # 发送 csvPath
        body_base = {"id": int(rule_id), "csvPath": base_path}
        body_base_bytes = json.dumps(body_base, ensure_ascii=False).encode("utf-8")

        if dry_run:
            _log(logger, log_mode, f"[update_rule_api:path] DRY_RUN PUT {url} model={model} id={rule_id} csvPath={base_path}")
        else:
            resp_code, resp_text = _http_put(
                url, body_base_bytes, headers=headers, timeout=timeout_sec, verify_tls=verify_tls
            )
            resp_one_line = _full_json_one_line(resp_text)
            success, msg, _data = _parse_api_fields(resp_text)

            if success is False:
                raise RuntimeError(f"[update_rule_api:path] model={model} id={rule_id} csvPath update failed: success=false msg={msg}")

            if 200 <= resp_code < 300:
                _log(logger, log_mode, f"[update_rule_api:path] OK model={model} id={rule_id} csvPath={base_path} code={resp_code}")
            else:
                raise RuntimeError(
                    f"[update_rule_api:path] FAILED model={model} id={rule_id} csvPath code={resp_code} resp={resp_one_line}"
                )

        # 发送 csvFullPath (仅当 enable_full=True 且 full_path 存在)
        if enable_full and full_path is not None:
            body_full = {"id": int(rule_id), "csvFullPath": full_path}
            body_full_bytes = json.dumps(body_full, ensure_ascii=False).encode("utf-8")

            if dry_run:
                _log(logger, log_mode, f"[update_rule_api:path] DRY_RUN PUT {url} model={model} id={rule_id} csvFullPath={full_path}")
            else:
                resp_code, resp_text = _http_put(
                    url, body_full_bytes, headers=headers, timeout=timeout_sec, verify_tls=verify_tls
                )
                resp_one_line = _full_json_one_line(resp_text)
                success, msg, _data = _parse_api_fields(resp_text)

                if success is False:
                    raise RuntimeError(f"[update_rule_api:path] model={model} id={rule_id} csvFullPath update failed: success=false msg={msg}")

                if 200 <= resp_code < 300:
                    _log(logger, log_mode, f"[update_rule_api:path] OK model={model} id={rule_id} csvFullPath={full_path} code={resp_code}")
                else:
                    raise RuntimeError(
                        f"[update_rule_api:path] FAILED model={model} id={rule_id} csvFullPath code={resp_code} resp={resp_one_line}"
                    )
