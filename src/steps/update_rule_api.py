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
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class UpdateApiResult:
    updated_models: List[str]


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
    解析接口标准响应：{"success": true/false, "msg": "...", "data": ...}
    返回 (success, msg, data)
    - success: True / False / None（无法解析或缺字段）
    - msg: str 或 None
    - data: 任意或 None
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

    models: List[str] = runtime["models"]

    presets = global_cfg["presets"]
    current_preset: str = str(step_cfg.get("current_preset", "dev"))
    base_url: str = str(presets[current_preset]["base_url"]).rstrip("/")

    # 从 global 读映射（dev/prod 各一套）
    model_to_rule_id = _build_model_to_id_map(global_cfg, current_preset)

    put_path: str = str(step_cfg.get("put_path", "/data-collector/rule/update"))
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

    # 可选：data=false 是否也当失败（默认 False：不改变你当前“只看 success”的语义）
    fail_if_data_false: bool = bool(step_cfg.get("fail_if_data_false", False))

    if not access_token:
        raise ValueError("[update_rule_api] access_token 为空（你要求写在配置文件中）")

    url = f"{base_url}{put_path}"

    updated: List[str] = []

    for model in models:
        rule_id = model_to_rule_id.get(model)

        if rule_id is None:
            msg = f"[update_rule_api] model={model} 缺少 rule_id 映射（preset={current_preset}）"
            if fail_if_id_missing:
                raise KeyError(msg)
            _log(logger, log_mode, msg)
            continue

        # 固定产物路径：csv_output/<model>/<model>_ranges.txt
        txt_path = repo_root / csv_output_dirname / model / f"{model}_ranges.txt"
        if not txt_path.exists():
            msg = f"[update_rule_api] model={model} 缺少 ranges.txt: {txt_path}"
            if fail_if_txt_missing:
                raise FileNotFoundError(msg)
            _log(logger, log_mode, msg)
            continue

        # 注意：pack_csv_txt 输出是 literal 单字符串（包含 \\n），这里必须原样作为 config 发送
        config_text = txt_path.read_text(encoding="utf-8", errors="strict")

        body_obj = {"id": int(rule_id), "config": config_text}
        body_bytes = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            # 按你接口要求：header 名叫 accesstoken（不要打印 token）
            "accesstoken": access_token,
        }

        if dry_run:
            _log(
                logger,
                log_mode,
                f"[update_rule_api] DRY_RUN PUT {url} model={model} id={rule_id} bytes={len(body_bytes)}",
            )
            updated.append(model)
            continue

        resp_code, resp_text = _http_put(
            url, body_bytes, headers=headers, timeout=timeout_sec, verify_tls=verify_tls
        )

        resp_one_line = _full_json_one_line(resp_text)
        success, msg, data = _parse_api_fields(resp_text)

        # 业务判定：success=false 直接报错（即使 HTTP=200），报错内容就是 msg
        if success is False:
            raise RuntimeError(str(msg) if msg else "success=false")

        # 可选：data=false 也当失败（很多接口用 data 表示是否真正生效）
        if fail_if_data_false and isinstance(data, bool) and data is False:
            # 仍然按你口径：报错内容用 msg（若没有 msg 就给一个兜底）
            raise RuntimeError(str(msg) if msg else "data=false")

        # HTTP 判定：非 2xx 报错（并打印完整 resp）
        if 200 <= resp_code < 300:
            _log(
                logger,
                log_mode,
                f"[update_rule_api] OK model={model} id={rule_id} code={resp_code} resp={resp_one_line}",
            )
            updated.append(model)
        else:
            raise RuntimeError(
                f"[update_rule_api] FAILED model={model} id={rule_id} code={resp_code} resp={resp_one_line}"
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

    兼容：即便 HTTP 非 2xx，也尽量读出 body 文本（便于日志/错误信息）。
    """
    req = urllib.request.Request(url=url, data=data, headers=headers, method="PUT")

    # NOTE: verify_tls 的完整关闭一般需要 SSLContext，这里保持最小依赖不实现。
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
