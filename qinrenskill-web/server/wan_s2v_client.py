"""
阿里云百炼 DashScope：wan2.2-s2v-detect（同步）与 wan2.2-s2v 异步视频合成、任务查询。
仅适用于中国内地（北京）地域 API Key。HTTP 使用 urllib，风格对齐 volc_tts。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

DASHSCOPE_BASE = (os.environ.get("DASHSCOPE_BASE_URL") or "").strip().rstrip("/") or (
    "https://dashscope.aliyuncs.com"
)
FACE_DETECT_PATH = "/api/v1/services/aigc/image2video/face-detect"
# 与官方 curl 示例一致（带尾斜杠，避免部分环境下 308 导致 POST 异常）
VIDEO_SYNTH_PATH = "/api/v1/services/aigc/image2video/video-synthesis/"


class WanS2VError(RuntimeError):
    pass


def _post_json(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    *,
    timeout_s: int = 120,
) -> Dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        raise WanS2VError(f"DashScope HTTP {e.code}：{err_body[:800]}") from e
    except urllib.error.URLError as e:
        raise WanS2VError(f"DashScope 网络错误：{e}") from e
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise WanS2VError(f"DashScope 返回非 JSON：{body[:300]}") from e


def _get_json(url: str, headers: Dict[str, str], *, timeout_s: int = 60) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        raise WanS2VError(f"DashScope HTTP {e.code}：{err_body[:800]}") from e
    except urllib.error.URLError as e:
        raise WanS2VError(f"DashScope 网络错误：{e}") from e
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise WanS2VError(f"DashScope 返回非 JSON：{body[:300]}") from e


def _auth_header(api_key: str) -> Dict[str, str]:
    key = (api_key or "").strip()
    if not key:
        raise WanS2VError("未配置 DASHSCOPE_API_KEY。")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def face_detect(api_key: str, image_url: str, *, timeout_s: int = 60) -> Tuple[bool, str]:
    """
    返回 (是否通过, 说明文案)。
    """
    url = f"{DASHSCOPE_BASE}{FACE_DETECT_PATH}"
    headers = _auth_header(api_key)
    payload = {
        "model": "wan2.2-s2v-detect",
        "input": {"image_url": image_url},
    }
    data = _post_json(url, headers, payload, timeout_s=timeout_s)
    out = data.get("output") or {}
    if isinstance(out, dict):
        ok = bool(out.get("check_pass"))
        if ok:
            return True, "检测通过"
        msg = out.get("message") or out.get("reason") or json.dumps(out, ensure_ascii=False)[:500]
        return False, str(msg) if msg else "检测未通过"
    return False, "检测响应异常"


def submit_video_task(
    api_key: str,
    *,
    image_url: str,
    audio_url: str,
    resolution: str = "480P",
    style: Optional[str] = None,
    timeout_s: int = 120,
) -> str:
    """
    异步创建 wan2.2-s2v 任务，返回 task_id。
    """
    url = f"{DASHSCOPE_BASE}{VIDEO_SYNTH_PATH}"
    headers = _auth_header(api_key)
    headers["X-DashScope-Async"] = "enable"
    parameters: Dict[str, Any] = {"resolution": resolution}
    if style:
        parameters["style"] = style
    payload: Dict[str, Any] = {
        "model": "wan2.2-s2v",
        "input": {
            "image_url": image_url,
            "audio_url": audio_url,
        },
        "parameters": parameters,
    }
    data = _post_json(url, headers, payload, timeout_s=timeout_s)
    out = data.get("output") or {}
    if not isinstance(out, dict):
        raise WanS2VError(f"创建任务响应异常：{data!r}")
    tid = out.get("task_id")
    if not tid or not isinstance(tid, str):
        raise WanS2VError(f"创建任务未返回 task_id：{data!r}")
    return tid


def get_task(api_key: str, task_id: str, *, timeout_s: int = 60) -> Dict[str, Any]:
    """查询任务状态与结果（原始 output 为主）。"""
    url = f"{DASHSCOPE_BASE}/api/v1/tasks/{task_id}"
    headers = _auth_header(api_key)
    data = _get_json(url, headers, timeout_s=timeout_s)
    return data


def parse_task_status(data: Dict[str, Any]) -> Tuple[str, Optional[str], Optional[str]]:
    """
    返回 (task_status, video_url_or_none, error_message_or_none)。
    """
    out = data.get("output") or {}
    if not isinstance(out, dict):
        return "UNKNOWN", None, "响应格式异常"
    status = str(out.get("task_status") or "UNKNOWN").upper()
    if status == "SUCCEEDED":
        results = out.get("results") or {}
        if isinstance(results, dict):
            vu = results.get("video_url")
            if isinstance(vu, str) and vu:
                return status, vu, None
        return status, None, "成功但缺少 video_url"
    if status in ("FAILED", "UNKNOWN"):
        msg = out.get("message") or out.get("code") or str(out)
        return status, None, str(msg)[:500]
    return status, None, None
