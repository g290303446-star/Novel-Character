"""
火山豆包 OpenSpeech HTTP 一次性合成（/api/v1/tts）。
请求/响应格式以官方 Demo 为准：https://www.volcengine.com/docs/6561/79820
"""

from __future__ import annotations

import base64
import json
import os
import uuid
import urllib.error
import urllib.request
from typing import Any, Dict


VOLC_TTS_URL = os.environ.get(
    "VOLC_TTS_URL", "https://openspeech.bytedance.com/api/v1/tts"
)


def _sanitize_cluster_label(s: str | None) -> str | None:
    """忽略误粘贴的说明文字、含中文的占位内容等。"""
    if s is None:
        return None
    t = s.strip()
    if not t:
        return None
    if any("\u4e00" <= c <= "\u9fff" for c in t):
        return None
    if "http://" in t.lower() or "https://" in t.lower():
        return None
    if "placeholder" in t.lower():
        return None
    return t


def _resolve_tts_cluster(*, param_cluster: str | None, voice_type: str) -> str:
    """
    请求体 app.cluster：大模型合成常用 volcano_tts；声音复刻 speaker（S_ 开头）需 volcano_icl（或并发 volcano_icl_concurr）。
    见豆包「声音复刻 / 大模型语音合成」文档中与 model_type、cluster 的对应关系。
    """
    vt = (voice_type or "").strip()
    p = _sanitize_cluster_label(param_cluster)
    env_c = _sanitize_cluster_label(os.environ.get("VOLC_TTS_CLUSTER"))

    if p:
        cluster = p
    elif env_c:
        cluster = env_c
    elif vt.startswith("S_"):
        cluster = (os.environ.get("VOLC_TTS_CLONE_CLUSTER") or "volcano_icl").strip()
    else:
        cluster = "volcano_tts"

    if vt.startswith("S_") and cluster == "volcano_tts":
        cluster = (os.environ.get("VOLC_TTS_CLONE_CLUSTER") or "volcano_icl").strip()
    return cluster


def _format_tts_http_error(http_code: int, err_body: str) -> str:
    """把原样响应附上简短可操作提示（适用于常见 403 + 3001 资源未授权）。"""
    snippet = (err_body or "").strip()[:500]
    hint = ""
    try:
        j: Dict[str, Any] = json.loads(err_body)
        biz_code = j.get("code")
        msg = j.get("message") or j.get("msg") or ""
        if not isinstance(msg, str):
            msg = str(msg)
        low = msg.lower()
        if biz_code == 3031 or "init engine instance failed" in low:
            hint = (
                " 【说明】常见于「cluster 与音色不匹配」。声音复刻返回的 speaker（S_ 开头）"
                "在 HTTP V1 接口中一般应使用 cluster=volcano_icl（并发版为 volcano_icl_concurr），"
                "而不是默认的 volcano_tts。请清空页面 cluster 后重试（服务端会自动选 volcano_icl），"
                "或在环境变量 VOLC_TTS_CLONE_CLUSTER 中指定。"
            )
        elif (
            biz_code == 3001
            or "requested resource not granted" in low
            or "resource not granted" in low
        ):
            hint = (
                " 【说明】这是火山侧「资源/权限未开通」类错误，不是 AppID/Token 写错时的典型报错。"
                "请到豆包语音控制台打开你的应用，确认已开通语音合成相关能力并勾选授权"
                "（例如「语音合成大模型」/ 大模型语音合成等，以控制台实际文案为准）；"
                "确认账户已开通计费、无欠费。"
                "若仍 403，请核对控制台文档中的 cluster、"
                "以及在环境变量 VOLC_TTS_VOICE_TYPE 中配置的音色是否与当前应用可用列表一致。"
                "官方说明可参考：豆包语音 API 接入 FAQ（resource not granted 条目）。"
            )
    except (json.JSONDecodeError, TypeError):
        pass
    return f"TTS HTTP {http_code}：{snippet}{hint}"


def volc_tts_configured() -> bool:
    app_id = (os.environ.get("VOLC_TTS_APP_ID") or "").strip()
    token = (os.environ.get("VOLC_TTS_ACCESS_TOKEN") or "").strip()
    return bool(app_id and token)


def synthesize_to_mp3_bytes(
    text: str,
    *,
    timeout_s: int = 60,
    app_id: str | None = None,
    access_token: str | None = None,
    voice_type: str | None = None,
    cluster: str | None = None,
) -> bytes:
    """
    返回 MP3 二进制（或 encoding 为 mp3 时的音频 bytes）。
    app_id / access_token 若传入则优先于环境变量（供单次请求透传；部署侧仍可只用环境变量）。
    """
    app_id_res = (app_id or os.environ.get("VOLC_TTS_APP_ID") or "").strip()
    token_res = (access_token or os.environ.get("VOLC_TTS_ACCESS_TOKEN") or "").strip()
    if not app_id_res or not token_res:
        raise RuntimeError("未配置 VOLC_TTS_APP_ID / VOLC_TTS_ACCESS_TOKEN。")

    app_id = app_id_res
    token = token_res
    voice_type = (
        voice_type or os.environ.get("VOLC_TTS_VOICE_TYPE") or "BV102_streaming"
    ).strip()
    cluster = _resolve_tts_cluster(param_cluster=cluster, voice_type=voice_type)
    encoding = (os.environ.get("VOLC_TTS_ENCODING") or "mp3").strip()
    try:
        speed_ratio = float(os.environ.get("VOLC_TTS_SPEED_RATIO", "1"))
    except ValueError:
        speed_ratio = 1.0
    try:
        volume_ratio = float(os.environ.get("VOLC_TTS_VOLUME_RATIO", "1"))
    except ValueError:
        volume_ratio = 1.0
    try:
        pitch_ratio = float(os.environ.get("VOLC_TTS_PITCH_RATIO", "1"))
    except ValueError:
        pitch_ratio = 1.0

    uid = (os.environ.get("VOLC_TTS_UID") or "qinrenskill_web").strip()

    payload: Dict[str, Any] = {
        "app": {
            "appid": app_id,
            "token": token,
            "cluster": cluster,
        },
        "user": {"uid": uid},
        "audio": {
            "voice_type": voice_type,
            "encoding": encoding,
            "speed_ratio": speed_ratio,
            "volume_ratio": volume_ratio,
            "pitch_ratio": pitch_ratio,
        },
        "request": {
            "reqid": str(uuid.uuid4()),
            "text": text.strip(),
            "text_type": "plain",
            "operation": "query",
        },
    }

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    # 官方常见写法：Authorization: Bearer;{access_token}（注意分号）
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer;{token}",
    }

    req = urllib.request.Request(
        VOLC_TTS_URL, data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw_text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        raise RuntimeError(_format_tts_http_error(e.code, err_body)) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"TTS 网络错误：{e}") from e

    try:
        data: Dict[str, Any] = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise RuntimeError("TTS 返回非 JSON。") from e

    code = data.get("code")
    if code not in (None, 0, 3000) and not data.get("data"):
        msg = data.get("message") or data.get("msg") or str(code)
        raise RuntimeError(f"TTS 失败：{msg}")

    b64 = data.get("data")
    if not b64 or not isinstance(b64, str):
        raise RuntimeError("TTS 响应缺少 data（base64）。")

    try:
        return base64.b64decode(b64, validate=False)
    except Exception as e:
        raise RuntimeError("TTS base64 解码失败。") from e
