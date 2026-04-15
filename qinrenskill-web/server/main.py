"""
单页 HTML + API：
- 左栏：可仅本地抽取《角色片段》.md，也可一键在服务端跑完整流水线（抽片段 → 分批归纳 → 档案 → 扮演指令）；
- 中栏：将 md 交给 DeepSeek 网页版 + 技能链后粘贴回本页（省本站 API）；
- 本站存会话并代理 /api/chat。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
import mimetypes
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator
from starlette.background import BackgroundTask

from app.deepseek_client import DeepSeekClient, DeepSeekError
from app.pipeline import _split_aliases, run_snippet_extractor
from server import build_jobs, quick_presets
from server.dialogue_sanitize import sanitize_spoken_dialogue
from server.session_store import ChatSession, SessionStore
from server.volc_tts import synthesize_to_mp3_bytes, volc_tts_configured
from server.lover_profiles import LoverProfile, merge_lover_profiles, profile_to_markdown
from server import lover_build_jobs
from server import wan_s2v_jobs

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "50")) * 1024 * 1024
SESSION_COOKIE = os.environ.get("SESSION_COOKIE_NAME", "qr_session")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
CHAT_MODEL = os.environ.get("CHAT_MODEL", "deepseek-chat")
CHAT_DIALOGUE_SANITIZE = os.environ.get("CHAT_DIALOGUE_SANITIZE", "0") == "1"
CHAT_SANITIZE_MODEL = os.environ.get("CHAT_SANITIZE_MODEL", "deepseek-chat")
CHAT_SANITIZE_MAX_TOKENS = int(os.environ.get("CHAT_SANITIZE_MAX_TOKENS", "1800"))
PASTED_PROMPT_MAX_CHARS = int(os.environ.get("PASTED_PROMPT_MAX_CHARS", "250000"))
PASTED_PROMPT_MIN_CHARS = int(os.environ.get("PASTED_PROMPT_MIN_CHARS", "20"))
SNIPPET_CONTEXT_LINES = int(os.environ.get("SNIPPET_CONTEXT_LINES", "20"))

app = FastAPI(title="qinrenskill memory web", version="0.3")
store = SessionStore(ttl_seconds=int(os.environ.get("SESSION_TTL_SECONDS", "86400")))

_quick_buckets: dict[str, tuple[float, int]] = {}
_QUICK_LIMIT = int(os.environ.get("QUICK_PER_MINUTE", "30"))
_paste_buckets: dict[str, tuple[float, int]] = {}
_PASTE_LIMIT = int(os.environ.get("PASTE_PER_MINUTE", "20"))
_snippet_buckets: dict[str, tuple[float, int]] = {}
_SNIPPET_LIMIT = int(os.environ.get("SNIPPET_FILE_PER_MINUTE", "12"))
_build_buckets: dict[str, tuple[float, int]] = {}
_BUILD_LIMIT = int(os.environ.get("BUILD_PER_MINUTE", "4"))
_BUILD_WINDOW = 60.0
_BUILD_EXECUTOR = ThreadPoolExecutor(max_workers=int(os.environ.get("BUILD_MAX_WORKERS", "2")))
_tts_buckets: dict[str, tuple[float, int]] = {}
_TTS_LIMIT = int(os.environ.get("TTS_PER_MINUTE", "30"))
_wan_s2v_buckets: dict[str, tuple[float, int]] = {}
_WAN_S2V_START_LIMIT = int(os.environ.get("WAN_S2V_START_PER_MINUTE", "8"))
_WAN_S2V_IMAGE_MAX_BYTES = int(os.environ.get("WAN_S2V_IMAGE_MAX_MB", "8")) * 1024 * 1024
_lover_questionnaire_buckets: dict[str, tuple[float, int]] = {}
_LOVER_QUESTIONNAIRE_LIMIT = int(os.environ.get("LOVER_QUESTIONNAIRE_PER_MINUTE", "10"))
_lover_build_buckets: dict[str, tuple[float, int]] = {}
_LOVER_BUILD_LIMIT = int(os.environ.get("LOVER_BUILD_PER_MINUTE", "4"))
_LOVER_BUILD_WINDOW = 60.0
_LOVER_BUILD_EXECUTOR = ThreadPoolExecutor(max_workers=int(os.environ.get("LOVER_BUILD_MAX_WORKERS", "2")))

logger = logging.getLogger(__name__)


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _quick_rate_allow(ip: str) -> bool:
    now = time.time()
    window_start, count = _quick_buckets.get(ip, (now, 0))
    if now - window_start > _BUILD_WINDOW:
        window_start, count = now, 0
    count += 1
    _quick_buckets[ip] = (window_start, count)
    return count <= _QUICK_LIMIT


def _paste_rate_allow(ip: str) -> bool:
    now = time.time()
    window_start, count = _paste_buckets.get(ip, (now, 0))
    if now - window_start > _BUILD_WINDOW:
        window_start, count = now, 0
    count += 1
    _paste_buckets[ip] = (window_start, count)
    return count <= _PASTE_LIMIT


def _snippet_rate_allow(ip: str) -> bool:
    now = time.time()
    window_start, count = _snippet_buckets.get(ip, (now, 0))
    if now - window_start > _BUILD_WINDOW:
        window_start, count = now, 0
    count += 1
    _snippet_buckets[ip] = (window_start, count)
    return count <= _SNIPPET_LIMIT


def _tts_rate_allow(ip: str) -> bool:
    now = time.time()
    window_start, count = _tts_buckets.get(ip, (now, 0))
    if now - window_start > _BUILD_WINDOW:
        window_start, count = now, 0
    count += 1
    _tts_buckets[ip] = (window_start, count)
    return count <= _TTS_LIMIT


def _wan_s2v_rate_allow(ip: str) -> bool:
    now = time.time()
    window_start, count = _wan_s2v_buckets.get(ip, (now, 0))
    if now - window_start > _BUILD_WINDOW:
        window_start, count = now, 0
    count += 1
    _wan_s2v_buckets[ip] = (window_start, count)
    return count <= _WAN_S2V_START_LIMIT


def _lover_questionnaire_rate_allow(ip: str) -> bool:
    now = time.time()
    window_start, count = _lover_questionnaire_buckets.get(ip, (now, 0))
    if now - window_start > _BUILD_WINDOW:
        window_start, count = now, 0
    count += 1
    _lover_questionnaire_buckets[ip] = (window_start, count)
    return count <= _LOVER_QUESTIONNAIRE_LIMIT


def _lover_build_rate_allow(ip: str) -> bool:
    now = time.time()
    window_start, count = _lover_build_buckets.get(ip, (now, 0))
    if now - window_start > _LOVER_BUILD_WINDOW:
        window_start, count = now, 0
    count += 1
    _lover_build_buckets[ip] = (window_start, count)
    return count <= _LOVER_BUILD_LIMIT


def _build_rate_allow(ip: str) -> bool:
    now = time.time()
    window_start, count = _build_buckets.get(ip, (now, 0))
    if now - window_start > _BUILD_WINDOW:
        window_start, count = now, 0
    count += 1
    _build_buckets[ip] = (window_start, count)
    return count <= _BUILD_LIMIT


def _html_path() -> Path:
    p = REPO_ROOT / "记忆系统MVP_1.html"
    if p.is_file():
        return p
    xs = list(REPO_ROOT.glob("*MVP_1.html"))
    if xs:
        return xs[0]
    return p


@app.get("/")
def index():
    return FileResponse(_html_path(), media_type="text/html; charset=utf-8")


@app.get("/lover")
def lover_page():
    p = REPO_ROOT / "lover.html"
    if p.is_file():
        return FileResponse(p, media_type="text/html; charset=utf-8")
    return Response(status_code=404)


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


def _attachment_content_disposition(filename_utf8: str) -> str:
    """
    HTTP 头价值必须是 latin-1；filename=\"...\" 段只能用 ASCII。
    中文等非 ASCII 文件名放在 filename*=UTF-8''...（RFC 5987）。
    """
    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", filename_utf8).strip("._-")
    ascii_fallback = re.sub(r"_+", "_", ascii_fallback) or "snippets"
    if not ascii_fallback.lower().endswith(".md"):
        ascii_fallback += ".md"
    return (
        f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{quote(filename_utf8)}'
    )


def _get_session_id(request: Request) -> Optional[str]:
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        return sid
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


class ChatBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=12000)


class VolcTtsBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=3000)
    # 仅当服务端未设置 VOLC_TTS_* 环境变量时使用；已配置环境变量时忽略下列字段
    volc_app_id: Optional[str] = Field(None, max_length=200)
    volc_access_token: Optional[str] = Field(None, max_length=4096)
    # 非敏感参数：允许页面指定音色/集群（例如声音复刻返回的 S_xxxxx）
    volc_voice_type: Optional[str] = Field(None, max_length=128)
    volc_cluster: Optional[str] = Field(None, max_length=64)


@app.post("/api/novel/snippets-file")
@app.post("/api/novel/snippets-file/")
async def novel_snippets_file(
    request: Request,
    novel: UploadFile = File(...),
    character_name: str = Form(...),
    aliases_text: str = Form(""),
):
    """
    仅跑本地抽取脚本，生成《角色片段》markdown，供用户下载后上传到 DeepSeek 网页版分析。
    不调用书内多批 LLM 归纳。
    """
    if not _snippet_rate_allow(_client_ip(request)):
        raise HTTPException(
            status_code=429,
            detail="生成片段文件过于频繁，请稍后再试。",
        )

    name = (character_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="缺少人物名称。")

    if not novel.filename or not novel.filename.lower().endswith(".txt"):
        raise HTTPException(status_code=400, detail="请上传 .txt 文件。")

    raw = await novel.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件过大。")

    tmp_root = tempfile.mkdtemp(prefix="novel_snip_")
    novel_path = os.path.join(tmp_root, "novel.txt")
    try:
        with open(novel_path, "wb") as f:
            f.write(raw)

        aliases = _split_aliases(aliases_text or "")
        try:
            out_path = run_snippet_extractor(
                novel_path=novel_path,
                character_name=name,
                aliases=aliases,
                output_dir=tmp_root,
                context_lines=SNIPPET_CONTEXT_LINES,
                require_quote=False,
            )
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        dl_name = os.path.basename(out_path)
        cd = _attachment_content_disposition(dl_name)

        # 流式读盘，避免整本合并片段时二次占满内存；发送完毕后再删临时目录。
        return FileResponse(
            out_path,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": cd},
            background=BackgroundTask(shutil.rmtree, tmp_root, True),
        )
    except HTTPException:
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise HTTPException(
            status_code=500,
            detail=f"生成片段文件时出错：{e}",
        ) from e


@app.post("/api/novel/build")
@app.post("/api/novel/build/")
async def novel_build(
    request: Request,
    novel: UploadFile = File(...),
    character_name: str = Form(...),
    aliases_text: str = Form(""),
    api_key: str = Form(...),
    chat_rules: str = Form(""),
):
    """
    后台线程跑完整流水线；用 GET /api/novel/build/{job_id} 轮询进度与结果。
    """
    if not _build_rate_allow(_client_ip(request)):
        raise HTTPException(
            status_code=429,
            detail="一键生成请求过于频繁，请稍后再试。",
        )

    name = (character_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="缺少人物名称。")

    key = (api_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="缺少 API Key。")

    if not novel.filename or not novel.filename.lower().endswith(".txt"):
        raise HTTPException(status_code=400, detail="请上传 .txt 文件。")

    raw = await novel.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件过大。")

    rules = (chat_rules or "").strip()
    pipeline_model = (os.environ.get("PIPELINE_MODEL") or "").strip() or None

    tmp_root = tempfile.mkdtemp(prefix="novel_build_")
    novel_path = os.path.join(tmp_root, "novel.txt")
    with open(novel_path, "wb") as f:
        f.write(raw)

    job_id = build_jobs.create_job(name)

    def _run() -> None:
        try:
            build_jobs.run_build_task(
                job_id=job_id,
                api_key=key,
                novel_path=novel_path,
                character_name=name,
                aliases_text=aliases_text or "",
                chat_rules=rules,
                model=pipeline_model or "",
            )
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

    loop = asyncio.get_running_loop()
    loop.run_in_executor(_BUILD_EXECUTOR, _run)
    return JSONResponse({"ok": True, "job_id": job_id})


@app.get("/api/novel/build/{job_id}")
def novel_build_status(job_id: str):
    st = build_jobs.get_job(job_id)
    if not st:
        raise HTTPException(
            status_code=404,
            detail="任务不存在或无效。",
        )
    out: dict = {
        "done": st.done,
        "error": st.error,
        "message": st.message,
        "percent": st.percent,
    }
    if st.done and not st.error and st.system_prompt:
        out["system_prompt"] = st.system_prompt
    return JSONResponse(out)


@app.post("/api/session/paste")
@app.post("/api/session/paste/")
async def session_paste(
    request: Request,
    api_key: str = Form(...),
    character_name: str = Form(...),
    pasted_system: str = Form(...),
    chat_rules: str = Form(""),
):
    if not _paste_rate_allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="创建会话过于频繁，请稍后重试。")

    key = (api_key or "").strip()
    name = (character_name or "").strip()
    body = (pasted_system or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="缺少 API Key。")
    if not name:
        raise HTTPException(status_code=400, detail="缺少人物名称。")
    if len(body) < PASTED_PROMPT_MIN_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"扮演指令过短（至少 {PASTED_PROMPT_MIN_CHARS} 字符）。",
        )

    rules = (chat_rules or "").strip()
    hidden = body
    if rules:
        hidden = hidden + "\n\n## 对话补充规则（用户指定）\n\n" + rules

    if len(hidden) > PASTED_PROMPT_MAX_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"扮演指令与补充规则合计超过上限（{PASTED_PROMPT_MAX_CHARS} 字符），请删减后重试。",
        )

    sid = store.create(
        hidden_system_prompt=hidden,
        api_key=key,
        chat_model=CHAT_MODEL,
    )
    resp = JSONResponse(
        {"ok": True, "character_name": name, "session_id": sid}
    )
    resp.set_cookie(
        key=SESSION_COOKIE,
        value=sid,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=store.ttl_seconds,
        path="/",
    )
    return resp


@app.post("/api/session/quick")
@app.post("/api/session/quick/")
async def session_quick(
    request: Request,
    api_key: str = Form(...),
    preset: str = Form("pavel"),
):
    if not _quick_rate_allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="快速体验请求过于频繁，请稍后重试。")

    key = (api_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="缺少 API Key。")

    try:
        hidden, character_name = quick_presets.resolve_preset(preset)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    sid = store.create(
        hidden_system_prompt=hidden,
        api_key=key,
        chat_model=CHAT_MODEL,
    )
    resp = JSONResponse(
        {
            "ok": True,
            "character_name": character_name,
            "session_id": sid,
            "preset": (preset or "").strip().lower(),
        }
    )
    resp.set_cookie(
        key=SESSION_COOKIE,
        value=sid,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=store.ttl_seconds,
        path="/",
    )
    return resp


@app.post("/api/chat")
async def chat(request: Request, body: ChatBody):
    sid = _get_session_id(request)
    if not sid:
        raise HTTPException(
            status_code=401,
            detail="无会话，请先粘贴扮演指令或快速体验创建会话。",
        )

    sess = store.get(sid)
    if not sess:
        raise HTTPException(status_code=401, detail="会话已过期，请重新创建。")

    text = body.text.strip()
    sess.messages.append({"role": "user", "content": text})

    max_msgs = int(os.environ.get("MAX_CHAT_MESSAGES", "40"))
    if len(sess.messages) > max_msgs:
        sess.messages = sess.messages[-max_msgs:]

    try:
        client = DeepSeekClient(api_key=sess.api_key, timeout_s=120, max_retries=3)
        out = client.chat_multi_turn(
            model=sess.chat_model,
            system=sess.hidden_system_prompt,
            messages=sess.messages,
            max_tokens=1500,
            temperature=0.35,
        )
        reply = out.content.strip()
        final_reply = reply
        if CHAT_DIALOGUE_SANITIZE and reply:
            try:
                cleaned = sanitize_spoken_dialogue(
                    client,
                    reply,
                    model=CHAT_SANITIZE_MODEL,
                    max_tokens=CHAT_SANITIZE_MAX_TOKENS,
                )
                if cleaned:
                    final_reply = cleaned
            except DeepSeekError:
                pass
        sess.messages.append({"role": "assistant", "content": final_reply})
        return JSONResponse({"reply": final_reply})
    except DeepSeekError as e:
        sess.messages.pop()
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/api/tts/volc")
@app.post("/api/tts/volc/")
async def tts_volc(request: Request, body: VolcTtsBody):
    """
    代理火山 HTTP TTS；需有效会话 Cookie。
    优先使用环境变量 VOLC_TTS_*；未配置时可在 JSON 中传 volc_app_id + volc_access_token（凭证经本站转发至火山，勿在不信任环境开启公网暴露）。
    """
    sid = _get_session_id(request)
    if not sid or not store.get(sid):
        raise HTTPException(
            status_code=401,
            detail="无有效会话，无法使用朗读。",
        )

    if not _tts_rate_allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="朗读请求过于频繁，请稍后再试。")

    txt = body.text.strip()
    client_app = (body.volc_app_id or "").strip()
    client_tok = (body.volc_access_token or "").strip()
    vt = (body.volc_voice_type or "").strip() or None
    cl = (body.volc_cluster or "").strip() or None

    try:
        if volc_tts_configured():
            audio = synthesize_to_mp3_bytes(txt, voice_type=vt, cluster=cl)
        else:
            if not client_app or not client_tok:
                raise HTTPException(
                    status_code=501,
                    detail="服务端未配置火山 TTS，请在页面填写 App ID 与 Access Token，或由运维配置 VOLC_TTS_APP_ID / VOLC_TTS_ACCESS_TOKEN。",
                )
            audio = synthesize_to_mp3_bytes(
                txt,
                app_id=client_app,
                access_token=client_tok,
                voice_type=vt,
                cluster=cl,
            )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    enc = (os.environ.get("VOLC_TTS_ENCODING") or "mp3").lower()
    media = (
        "audio/mpeg"
        if enc == "mp3"
        else "audio/wav"
        if enc in ("wav", "pcm")
        else "application/octet-stream"
    )
    return Response(content=audio, media_type=media)


def _form_truthy(v: Optional[str]) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _wan_s2v_last_assistant_text(sess: ChatSession) -> Optional[str]:
    for m in reversed(sess.messages):
        if m.get("role") == "assistant":
            c = (m.get("content") or "").strip()
            if c:
                return c
    return None


def _wan_s2v_allowed_image_suffix(name: str) -> Optional[str]:
    n = (name or "").lower()
    for suf in (".jpg", ".jpeg", ".png", ".bmp", ".webp"):
        if n.endswith(suf):
            return suf
    return None


@app.get("/api/mvp/wan-s2v/asset/{token}/image")
async def wan_s2v_asset_image(token: str):
    """公网可访问，供阿里云拉取图片（无 Cookie）。"""
    p = wan_s2v_jobs.get_asset_file(token, "image")
    if not p:
        raise HTTPException(status_code=404, detail="资源不存在或已过期。")
    mt = mimetypes.guess_type(str(p))[0] or "image/jpeg"
    return FileResponse(p, media_type=mt)


@app.get("/api/mvp/wan-s2v/asset/{token}/audio")
async def wan_s2v_asset_audio(token: str):
    """公网可访问，供阿里云拉取音频（无 Cookie）。"""
    p = wan_s2v_jobs.get_asset_file(token, "audio")
    if not p:
        raise HTTPException(status_code=404, detail="资源不存在或已过期。")
    enc = (os.environ.get("VOLC_TTS_ENCODING") or "mp3").lower()
    mt = "audio/mpeg" if enc == "mp3" else "audio/wav" if enc in ("wav", "pcm") else "application/octet-stream"
    return FileResponse(p, media_type=mt)


@app.post("/api/mvp/wan-s2v/start")
@app.post("/api/mvp/wan-s2v/start/")
async def wan_s2v_start(
    request: Request,
    image: UploadFile = File(...),
    spoken_text: str = Form(""),
    llm_prompt: str = Form(""),
    use_last_assistant_reply: str = Form("1"),
    resolution: str = Form("480P"),
    style: str = Form(""),
    volc_app_id: Optional[str] = Form(None),
    volc_access_token: Optional[str] = Form(None),
    volc_voice_type: Optional[str] = Form(None),
    volc_cluster: Optional[str] = Form(None),
):
    """
    会话内：口播来源优先级为「显式口播文案」>「llm_prompt 现写台词」>「上一轮助手回复 + use_last_assistant_reply」；
    再经火山 TTS → 公网 URL → wan2.2-s2v。
    需配置 WAN_S2V_PUBLIC_BASE_URL 与 DASHSCOPE_API_KEY（北京地域）。
    """
    sid = _get_session_id(request)
    if not sid or not store.get(sid):
        raise HTTPException(status_code=401, detail="无有效会话。")

    if not _wan_s2v_rate_allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="数字人任务提交过于频繁，请稍后再试。")

    public_base = (os.environ.get("WAN_S2V_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if not public_base:
        raise HTTPException(
            status_code=503,
            detail="服务端未配置 WAN_S2V_PUBLIC_BASE_URL（需阿里云可访问的站点根 URL，无尾斜杠）。",
        )
    dash_key = (os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    if not dash_key:
        raise HTTPException(
            status_code=503,
            detail="服务端未配置 DASHSCOPE_API_KEY（北京地域百炼 API Key）。",
        )

    if not image.filename:
        raise HTTPException(status_code=400, detail="请上传图片文件。")
    suf = _wan_s2v_allowed_image_suffix(image.filename)
    if not suf:
        raise HTTPException(
            status_code=400,
            detail="图片仅支持 jpg / jpeg / png / bmp / webp。",
        )

    raw_img = await image.read()
    if len(raw_img) > _WAN_S2V_IMAGE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="图片过大（MVP 限制 8MB 以内）。")

    st = (spoken_text or "").strip()
    lp = (llm_prompt or "").strip()
    use_last = _form_truthy(use_last_assistant_reply)
    sess = store.get(sid)
    assert sess is not None

    if st:
        if len(st) > wan_s2v_jobs.WAN_SPOKEN_MAX_CHARS:
            raise HTTPException(
                status_code=400,
                detail=f"口播文案过长（不超过 {wan_s2v_jobs.WAN_SPOKEN_MAX_CHARS} 字，且音频需远短于 20 秒以满足 wan2.2-s2v 限制）。",
            )
        spoken = st
    elif lp:
        user_msg = (
            "请根据角色设定，写一段用于数字人口播的台词。\n"
            "要求：只输出要说的中文内容，不要标题、引号、括号说明或动作描写。\n\n"
            f"{lp}"
        )
        try:
            client = DeepSeekClient(api_key=sess.api_key, timeout_s=120, max_retries=2)
            out = client.chat_completions(
                model=sess.chat_model,
                system=sess.hidden_system_prompt,
                user=user_msg,
                max_tokens=600,
                temperature=0.35,
            )
            spoken = out.content.strip()
        except DeepSeekError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        if not spoken:
            raise HTTPException(status_code=502, detail="模型未返回可用台词。")
        if len(spoken) > wan_s2v_jobs.WAN_SPOKEN_MAX_CHARS:
            spoken = spoken[: wan_s2v_jobs.WAN_SPOKEN_MAX_CHARS]
    elif use_last:
        last = _wan_s2v_last_assistant_text(sess)
        if not last:
            raise HTTPException(
                status_code=400,
                detail="当前会话还没有助手回复，请先对话一轮，或改选手动口播 / AI 生成台词。",
            )
        if len(last) > wan_s2v_jobs.WAN_SPOKEN_MAX_CHARS:
            spoken = last[: wan_s2v_jobs.WAN_SPOKEN_MAX_CHARS]
        else:
            spoken = last
    else:
        raise HTTPException(
            status_code=400,
            detail="请填写口播文案、或填写 AI 生成提示、或勾选「使用上一轮助手回复」。",
        )

    client_app = (volc_app_id or "").strip()
    client_tok = (volc_access_token or "").strip()
    vt = (volc_voice_type or "").strip() or None
    cl = (volc_cluster or "").strip() or None
    try:
        if volc_tts_configured():
            audio = synthesize_to_mp3_bytes(spoken, voice_type=vt, cluster=cl)
        else:
            if not client_app or not client_tok:
                raise HTTPException(
                    status_code=501,
                    detail="服务端未配置火山 TTS，请在表单中提供 volc_app_id 与 volc_access_token。",
                )
            audio = synthesize_to_mp3_bytes(
                spoken,
                app_id=client_app,
                access_token=client_tok,
                voice_type=vt,
                cluster=cl,
            )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    enc = (os.environ.get("VOLC_TTS_ENCODING") or "mp3").lower()
    audio_name = "audio.mp3" if enc == "mp3" else "audio.wav" if enc in ("wav", "pcm") else "audio.bin"

    token = uuid.uuid4().hex
    job_dir = REPO_ROOT / "data" / "wan_s2v" / token
    job_dir.mkdir(parents=True, exist_ok=True)
    image_name = f"portrait{suf}"
    try:
        with open(job_dir / image_name, "wb") as f:
            f.write(raw_img)
        with open(job_dir / audio_name, "wb") as f:
            f.write(audio)
    except OSError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"写入临时文件失败：{e}") from e

    wan_s2v_jobs.register_asset(
        token,
        job_dir,
        image_name=image_name,
        audio_name=audio_name,
    )

    image_url = f"{public_base}/api/mvp/wan-s2v/asset/{token}/image"
    audio_url = f"{public_base}/api/mvp/wan-s2v/asset/{token}/audio"

    res = (resolution or "480P").strip().upper()
    if res not in ("480P", "720P"):
        shutil.rmtree(job_dir, ignore_errors=True)
        wan_s2v_jobs.unregister_asset(token)
        raise HTTPException(status_code=400, detail="resolution 仅支持 480P 或 720P。")

    sty = (style or "").strip() or None
    env_style = (os.environ.get("WAN_S2V_STYLE") or "").strip() or None
    style_final = sty or env_style

    jid = wan_s2v_jobs.create_job()
    if not wan_s2v_jobs.begin_slot(jid):
        shutil.rmtree(job_dir, ignore_errors=True)
        wan_s2v_jobs.unregister_asset(token)
        wan_s2v_jobs.delete_job(jid)
        raise HTTPException(
            status_code=429,
            detail="已有数字人视频任务进行中（阿里云同时仅 1 路），请待完成后再试。",
        )

    wan_s2v_jobs.spawn_wan_worker(
        jid,
        dashscope_api_key=dash_key,
        image_url=image_url,
        audio_url=audio_url,
        job_dir=job_dir,
        token=token,
        resolution=res,
        style=style_final,
        spoken_text=spoken,
    )
    return JSONResponse({"job_id": jid, "spoken_text": spoken})


@app.get("/api/mvp/wan-s2v/job/{job_id}")
@app.get("/api/mvp/wan-s2v/job/{job_id}/")
async def wan_s2v_job_status(job_id: str):
    st = wan_s2v_jobs.get_job(job_id)
    if not st:
        raise HTTPException(status_code=404, detail="任务不存在。")
    return JSONResponse(
        {
            "status": st.status,
            "message": st.message,
            "video_url": st.video_url,
            "error": st.error,
            "spoken_text": st.spoken_text,
            "dashscope_task_id": st.dashscope_task_id,
        }
    )


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    sid = _get_session_id(request)
    if sid:
        store.delete(sid)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return JSONResponse({"ok": True})


# ========================
# 恋人专属 API
# ========================


def _normalize_str_dict(v: object) -> Dict[str, str]:
    """将 JSON 中的对象规整为 Dict[str, str]（null/数字等转为字符串），避免校验失败或下游类型异常。"""
    if v is None:
        return {}
    if not isinstance(v, dict):
        return {}
    out: Dict[str, str] = {}
    for k, val in v.items():
        key = str(k).strip() if k is not None else ""
        if not key:
            continue
        if val is None:
            out[key] = ""
        elif isinstance(val, str):
            out[key] = val
        else:
            out[key] = str(val)
    return out


def _normalize_str_list(v: object) -> List[str]:
    """列表字段支持逗号分隔字符串、null、非列表输入，减少恢复的旧备份导致 422。"""
    if v is None:
        return []
    if isinstance(v, str):
        parts = [s.strip() for s in v.replace("，", ",").split(",")]
        return [p for p in parts if p]
    if not isinstance(v, list):
        return []
    out: List[str] = []
    for x in v:
        if x is None:
            out.append("")
        elif isinstance(x, str):
            out.append(x)
        else:
            out.append(str(x))
    return out


def _normalize_love_languages(v: object) -> List[Dict[str, str]]:
    if v is None:
        return []
    if not isinstance(v, list):
        return []
    rows: List[Dict[str, str]] = []
    for item in v:
        if item is None or not isinstance(item, dict):
            continue
        row: Dict[str, str] = {}
        for k, val in item.items():
            kk = str(k) if k is not None else ""
            if val is None:
                row[kk] = ""
            elif isinstance(val, str):
                row[kk] = val
            else:
                row[kk] = str(val)
        rows.append(row)
    return rows


def _coerce_lover_questionnaire_payload(data: object) -> object:
    """在 Pydantic 校验前规整 body，兼容备份 JSON / 手改字段导致的边缘类型。"""
    if not isinstance(data, dict):
        return data
    o = dict(data)
    str_fields = (
        "persona_name",
        "pronouns",
        "age_feel",
        "relationship_start",
        "intimacy_level",
        "emotional_closeness",
        "physical_textual_expression",
        "primary_love_language",
        "secondary_love_language",
        "primary_role",
        "initiative_tendency",
        "role_dynamics",
        "argument_style",
        "repair_style",
        "romantic_voice_style",
        "humor_style",
        "compliment_style",
        "physical_description",
        "style_fashion",
        "distinctive_features",
        "profession",
        "life_philosophy",
    )
    for k in str_fields:
        if k not in o:
            continue
        val = o[k]
        if val is None:
            o[k] = ""
        elif not isinstance(val, str):
            o[k] = str(val)
    o["emotional_expression"] = _normalize_str_dict(o.get("emotional_expression"))
    o["daily_rhythm"] = _normalize_str_dict(o.get("daily_rhythm"))
    list_fields = (
        "dealbreakers",
        "endearment_terms",
        "personality_traits",
        "values",
        "quirks",
        "secondary_roles",
        "hard_boundaries",
        "soft_boundaries",
        "do_not_infer",
        "open_questions",
        "interests",
    )
    for k in list_fields:
        o[k] = _normalize_str_list(o.get(k))
    o["love_languages"] = _normalize_love_languages(o.get("love_languages"))
    return o


class LoverQuestionnaireBody(BaseModel):
    persona_name: str = Field(..., min_length=1, max_length=50)
    pronouns: str = "ta"
    age_feel: str = ""
    relationship_start: str = ""
    intimacy_level: str = ""
    emotional_closeness: str = ""
    physical_textual_expression: str = ""
    primary_love_language: str = ""
    secondary_love_language: str = ""
    love_languages: List[Dict[str, str]] = []
    primary_role: str = ""
    secondary_roles: List[str] = []
    initiative_tendency: str = ""
    role_dynamics: str = ""
    emotional_expression: Dict[str, str] = {}
    argument_style: str = ""
    repair_style: str = ""
    dealbreakers: List[str] = []
    romantic_voice_style: str = ""
    humor_style: str = ""
    compliment_style: str = ""
    endearment_terms: List[str] = []
    daily_rhythm: Dict[str, str] = {}
    personality_traits: List[str] = []
    values: List[str] = []
    quirks: List[str] = []
    physical_description: str = ""
    style_fashion: str = ""
    distinctive_features: str = ""
    profession: str = ""
    interests: List[str] = []
    life_philosophy: str = ""
    hard_boundaries: List[str] = []
    soft_boundaries: List[str] = []
    do_not_infer: List[str] = []
    open_questions: List[str] = []

    @model_validator(mode="before")
    @classmethod
    def _coerce_questionnaire_fields(cls, data: object):
        return _coerce_lover_questionnaire_payload(data)


_lover_profiles_store: Dict[str, LoverProfile] = {}


@app.post("/api/lover/questionnaire")
@app.post("/api/lover/questionnaire/")
async def lover_questionnaire(request: Request, body: LoverQuestionnaireBody):
    """提交问卷，生成恋人档案。"""
    if not _lover_questionnaire_rate_allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试。")

    if not (body.persona_name or "").strip():
        raise HTTPException(status_code=400, detail="缺少恋人名称。")

    try:
        profile = LoverProfile(
            persona_name=body.persona_name.strip(),
            pronouns=body.pronouns or "ta",
            age_feel=body.age_feel,
            relationship_start=body.relationship_start,
            sources=["questionnaire"],
            intimacy_level=body.intimacy_level,
            emotional_closeness=body.emotional_closeness,
            physical_textual_expression=body.physical_textual_expression,
            primary_love_language=body.primary_love_language,
            secondary_love_language=body.secondary_love_language,
            love_languages=body.love_languages,
            primary_role=body.primary_role,
            secondary_roles=body.secondary_roles,
            initiative_tendency=body.initiative_tendency,
            role_dynamics=body.role_dynamics,
            emotional_expression=body.emotional_expression,
            argument_style=body.argument_style,
            repair_style=body.repair_style,
            dealbreakers=body.dealbreakers,
            romantic_voice_style=body.romantic_voice_style,
            humor_style=body.humor_style,
            compliment_style=body.compliment_style,
            endearment_terms=body.endearment_terms,
            daily_rhythm=body.daily_rhythm,
            personality_traits=body.personality_traits,
            values=body.values,
            quirks=body.quirks,
            physical_description=body.physical_description,
            style_fashion=body.style_fashion,
            distinctive_features=body.distinctive_features,
            profession=body.profession,
            interests=body.interests,
            life_philosophy=body.life_philosophy,
            hard_boundaries=body.hard_boundaries,
            soft_boundaries=body.soft_boundaries,
            do_not_infer=body.do_not_infer,
            open_questions=body.open_questions,
        )
        name_for_id = body.persona_name.strip()
        slug = "".join(
            c for c in name_for_id[:24] if c.isalnum() or c in ("-", "_")
        ) or "p"
        profile_id = f"lover_{int(time.time() * 1000)}_{slug}"
        _lover_profiles_store[profile_id] = profile
    except HTTPException:
        raise
    except Exception:
        logger.exception("lover_questionnaire: 构建档案或写入缓存失败")
        raise HTTPException(
            status_code=500,
            detail="提交问卷时服务器内部错误，请稍后重试。若仍失败，请查看运行本服务的终端是否有报错并反馈。",
        ) from None

    return JSONResponse({"ok": True, "profile_id": profile_id})


@app.get("/api/lover/questionnaire/{profile_id}")
def lover_questionnaire_get(profile_id: str):
    profile = _lover_profiles_store.get(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="档案不存在。")
    return JSONResponse({"ok": True, "profile": profile_to_markdown(profile)})


@app.post("/api/lover/novel/build")
@app.post("/api/lover/novel/build/")
async def lover_novel_build(
    request: Request,
    novel: UploadFile = File(...),
    character_name: str = Form(...),
    aliases_text: str = Form(""),
    api_key: str = Form(...),
    chat_rules: str = Form(""),
):
    """从小说提取恋人特质（异步）。"""
    if not _lover_build_rate_allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试。")

    name = (character_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="缺少人物名称。")

    key = (api_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="缺少 API Key。")

    if not novel.filename or not novel.filename.lower().endswith(".txt"):
        raise HTTPException(status_code=400, detail="请上传 .txt 文件。")

    raw = await novel.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件过大。")

    rules = (chat_rules or "").strip()
    pipeline_model = (os.environ.get("PIPELINE_MODEL") or "").strip() or None

    tmp_root = tempfile.mkdtemp(prefix="lover_build_")
    novel_path = os.path.join(tmp_root, "novel.txt")
    with open(novel_path, "wb") as f:
        f.write(raw)

    job_id = lover_build_jobs.create_lover_job(name)

    def _run() -> None:
        try:
            lover_build_jobs.run_lover_novel_build_task(
                job_id=job_id,
                api_key=key,
                novel_path=novel_path,
                character_name=name,
                aliases_text=aliases_text or "",
                chat_rules=rules,
                model=pipeline_model or "",
                output_dir=tmp_root,
            )
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

    loop = asyncio.get_running_loop()
    loop.run_in_executor(_LOVER_BUILD_EXECUTOR, _run)
    return JSONResponse({"ok": True, "job_id": job_id})


@app.get("/api/lover/novel/build/{job_id}")
def lover_novel_build_status(job_id: str):
    st = lover_build_jobs.get_lover_job(job_id)
    if not st:
        raise HTTPException(status_code=404, detail="任务不存在或无效。")
    out: dict = {
        "done": st.done,
        "error": st.error,
        "message": st.message,
        "percent": st.percent,
    }
    if st.done and not st.error and st.system_prompt:
        out["system_prompt"] = st.system_prompt
        out["extraction_output"] = st.extraction_output
    return JSONResponse(out)


class LoverCompileBody(BaseModel):
    profile_id: str = Field(..., min_length=1)
    api_key: str = Field(..., min_length=1)
    chat_rules: str = ""
    model: str = ""


@app.post("/api/lover/compile")
@app.post("/api/lover/compile/")
async def lover_compile(request: Request, body: LoverCompileBody):
    """将恋人档案编译为系统提示词。"""
    profile = _lover_profiles_store.get(body.profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="档案不存在。")

    key = (body.api_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="缺少 API Key。")

    model = (body.model or "").strip() or os.environ.get("CHAT_MODEL", "deepseek-chat")
    archive_md = profile_to_markdown(profile)

    client = DeepSeekClient(api_key=key, timeout_s=120, max_retries=3)
    try:
        out = client.chat_completions(
            model=model,
            system="你是恋人扮演指令编译器。你必须根据用户提供的恋人档案生成 system prompt。"
            " HardBoundaries 优先级最高；AllowedFacts 只能来自档案。"
            " 输出必须是完整 markdown，结构参考 lover-system-prompt 模板。",
            user=f"请把以下恋人档案编译成系统提示词。\n\n"
            f"档案：\n```markdown\n{archive_md}\n```\n\n"
            "要求：\n"
            "- VoiceRules、LoveLanguageRules、EmotionalExpression、ConflictResolution、DailyRhythm 等章节必须填写。\n"
            "- 不新增档案中未提到的背景细节。\n"
            "- 包含 EthicalGuidelines 章节。\n"
            "- 直接输出编译结果。",
            max_tokens=3000,
            temperature=0.3,
        )
        system_prompt = out.content.strip()
        rules = (body.chat_rules or "").strip()
        if rules:
            system_prompt += "\n\n## 对话补充规则（用户指定）\n\n" + rules
        return JSONResponse({"ok": True, "system_prompt": system_prompt})
    except DeepSeekError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/api/session/lover")
@app.post("/api/session/lover/")
async def session_lover(
    request: Request,
    api_key: str = Form(...),
    character_name: str = Form(...),
    system_prompt: str = Form(...),
    chat_rules: str = Form(""),
):
    """创建恋人聊天会话。"""
    if not _paste_rate_allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="创建会话过于频繁，请稍后重试。")

    key = (api_key or "").strip()
    name = (character_name or "").strip()
    prompt = (system_prompt or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="缺少 API Key。")
    if not name:
        raise HTTPException(status_code=400, detail="缺少恋人名称。")
    if len(prompt) < PASTED_PROMPT_MIN_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"扮演指令过短（至少 {PASTED_PROMPT_MIN_CHARS} 字符）。",
        )

    rules = (chat_rules or "").strip()
    hidden = prompt
    if rules:
        hidden = hidden + "\n\n## 对话补充规则（用户指定）\n\n" + rules

    if len(hidden) > PASTED_PROMPT_MAX_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"扮演指令与补充规则合计超过上限（{PASTED_PROMPT_MAX_CHARS} 字符）。",
        )

    sid = store.create(
        hidden_system_prompt=hidden,
        api_key=key,
        chat_model=CHAT_MODEL,
    )
    resp = JSONResponse({"ok": True, "character_name": name, "session_id": sid})
    resp.set_cookie(
        key=SESSION_COOKIE,
        value=sid,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=store.ttl_seconds,
        path="/",
    )
    return resp
