"""
数字人 wan2.2-s2v MVP：内存任务状态、单路并发槽、后台线程（检测 → 提交 → 轮询）。
"""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from server.wan_s2v_client import (
    WanS2VError,
    face_detect,
    get_task,
    parse_task_status,
    submit_video_task,
)

# 官方：音频时长需 <20s；口播过长会导致失败或浪费
WAN_SPOKEN_MAX_CHARS = int(
    __import__("os").environ.get("WAN_S2V_MAX_SPOKEN_CHARS", "400")
)
POLL_INTERVAL_S = float(
    __import__("os").environ.get("WAN_S2V_POLL_INTERVAL_S", "15")
)
POLL_TIMEOUT_S = float(
    __import__("os").environ.get("WAN_S2V_POLL_TIMEOUT_S", "900")
)


@dataclass
class WanJobState:
    status: str = "queued"  # queued running succeeded failed
    message: str = ""
    video_url: Optional[str] = None
    error: Optional[str] = None
    spoken_text: str = ""
    dashscope_task_id: Optional[str] = None


_jobs: Dict[str, WanJobState] = {}
_jobs_lock = threading.Lock()

# 阿里云「同时处理中任务数量」为 1：本站同一时刻只跑一条 wan 流水线
_slot_lock = threading.Lock()
_active_slot_job_id: Optional[str] = None

# token -> { "dir": Path, "image_name": str, "audio_name": str }
_assets: Dict[str, Dict[str, Any]] = {}
_assets_lock = threading.Lock()


def begin_slot(job_id: str) -> bool:
    global _active_slot_job_id
    with _slot_lock:
        if _active_slot_job_id is not None:
            return False
        _active_slot_job_id = job_id
        return True


def end_slot(job_id: str) -> None:
    global _active_slot_job_id
    with _slot_lock:
        if _active_slot_job_id == job_id:
            _active_slot_job_id = None


def register_asset(
    token: str,
    job_dir: Path,
    *,
    image_name: str,
    audio_name: str,
) -> None:
    with _assets_lock:
        _assets[token] = {
            "dir": job_dir,
            "image_name": image_name,
            "audio_name": audio_name,
        }


def unregister_asset(token: str) -> None:
    with _assets_lock:
        _assets.pop(token, None)


def get_asset_file(token: str, kind: str) -> Optional[Path]:
    """kind: image | audio"""
    with _assets_lock:
        rec = _assets.get(token)
    if not rec:
        return None
    d = rec["dir"]
    if kind == "image":
        name = rec.get("image_name")
    elif kind == "audio":
        name = rec.get("audio_name")
    else:
        return None
    if not name:
        return None
    p = Path(d) / name
    if p.is_file():
        return p
    return None


def create_job() -> str:
    jid = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[jid] = WanJobState()
    return jid


def delete_job(job_id: str) -> None:
    with _jobs_lock:
        _jobs.pop(job_id, None)


def get_job(job_id: str) -> Optional[WanJobState]:
    with _jobs_lock:
        st = _jobs.get(job_id)
        if st is None:
            return None
        return WanJobState(
            status=st.status,
            message=st.message,
            video_url=st.video_url,
            error=st.error,
            spoken_text=st.spoken_text,
            dashscope_task_id=st.dashscope_task_id,
        )


def _update_job(job_id: str, **kwargs: Any) -> None:
    with _jobs_lock:
        st = _jobs.get(job_id)
        if not st:
            return
        for k, v in kwargs.items():
            setattr(st, k, v)


def _cleanup_job_files(job_dir: Path, token: str) -> None:
    unregister_asset(token)
    try:
        shutil.rmtree(job_dir, ignore_errors=True)
    except Exception:
        pass


def run_wan_pipeline(
    job_id: str,
    *,
    dashscope_api_key: str,
    image_url: str,
    audio_url: str,
    job_dir: Path,
    token: str,
    resolution: str,
    style: Optional[str],
    spoken_text: str,
) -> None:
    try:
        _update_job(
            job_id,
            status="running",
            message="图像检测中…",
            spoken_text=spoken_text,
        )
        ok, detect_msg = face_detect(dashscope_api_key, image_url)
        if not ok:
            _update_job(
                job_id,
                status="failed",
                error=f"图像检测未通过：{detect_msg}",
                message="失败",
            )
            return

        _update_job(job_id, message="已提交视频生成任务…")
        try:
            tid = submit_video_task(
                dashscope_api_key,
                image_url=image_url,
                audio_url=audio_url,
                resolution=resolution,
                style=style,
            )
        except WanS2VError as e:
            _update_job(
                job_id,
                status="failed",
                error=str(e),
                message="失败",
            )
            return

        _update_job(job_id, dashscope_task_id=tid, message="排队/生成中（约数分钟）…")

        deadline = time.monotonic() + POLL_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_S)
            try:
                raw = get_task(dashscope_api_key, tid)
            except WanS2VError as e:
                _update_job(
                    job_id,
                    status="failed",
                    error=f"查询任务失败：{e}",
                    message="失败",
                )
                return
            status, video_url, err = parse_task_status(raw)
            if status == "SUCCEEDED" and video_url:
                _update_job(
                    job_id,
                    status="succeeded",
                    video_url=video_url,
                    message="完成",
                )
                return
            if status in ("FAILED", "UNKNOWN"):
                _update_job(
                    job_id,
                    status="failed",
                    error=err or status,
                    message="失败",
                )
                return
            _update_job(job_id, message=f"生成中（{status}）…")

        _update_job(
            job_id,
            status="failed",
            error="等待结果超时，请稍后在百炼控制台查看任务。",
            message="超时",
        )
    finally:
        _cleanup_job_files(job_dir, token)
        end_slot(job_id)


def spawn_wan_worker(
    job_id: str,
    *,
    dashscope_api_key: str,
    image_url: str,
    audio_url: str,
    job_dir: Path,
    token: str,
    resolution: str,
    style: Optional[str],
    spoken_text: str,
) -> None:
    t = threading.Thread(
        target=run_wan_pipeline,
        kwargs={
            "job_id": job_id,
            "dashscope_api_key": dashscope_api_key,
            "image_url": image_url,
            "audio_url": audio_url,
            "job_dir": job_dir,
            "token": token,
            "resolution": resolution,
            "style": style,
            "spoken_text": spoken_text,
        },
        daemon=True,
    )
    t.start()
