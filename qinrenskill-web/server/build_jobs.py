"""
内存中的「一键生成扮演指令」任务状态，供轮询查询进度。
"""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from server.pipeline_service import generate_hidden_system_prompt


@dataclass
class BuildJobState:
    done: bool = False
    error: Optional[str] = None
    message: str = ""
    percent: int = 0
    system_prompt: Optional[str] = None
    character_name: str = ""


_jobs: dict[str, BuildJobState] = {}
_lock = threading.Lock()


def create_job(character_name: str = "") -> str:
    jid = uuid.uuid4().hex
    with _lock:
        _jobs[jid] = BuildJobState(character_name=character_name or "")
    return jid


def get_job(job_id: str) -> Optional[BuildJobState]:
    with _lock:
        st = _jobs.get(job_id)
        if st is None:
            return None
        return BuildJobState(
            done=st.done,
            error=st.error,
            message=st.message,
            percent=st.percent,
            system_prompt=st.system_prompt,
            character_name=st.character_name,
        )


def _update_job(job_id: str, **kwargs) -> None:
    with _lock:
        st = _jobs.get(job_id)
        if not st:
            return
        for k, v in kwargs.items():
            setattr(st, k, v)


def make_status_cb(job_id: str) -> Callable[[str], None]:
    """根据 pipeline 中文 status 文案推算百分比（估算，非严格 ETA）。"""
    batch_total: Optional[int] = None

    def cb(msg: str) -> None:
        nonlocal batch_total
        pct: Optional[int] = None
        if "初始化" in msg:
            pct = 3
        elif "步骤 1/3" in msg:
            pct = 12
        elif "步骤 2/3" in msg and "渲染" not in msg and "归纳" in msg:
            pct = 18
        elif "片段总数" in msg and "批" in msg:
            m = re.search(r"将分\s*(\d+)\s*批", msg)
            if m:
                batch_total = int(m.group(1))
            pct = 22
        elif "归纳中" in msg:
            m = re.search(r"第\s*(\d+)/(\d+)\s*批", msg)
            if m:
                i, n = int(m.group(1)), int(m.group(2))
                batch_total = n
                span = 55
                pct = 22 + int((i / max(n, 1)) * span)
        elif "渲染人物档案" in msg:
            pct = 82
        elif "步骤 3/3" in msg:
            pct = 93
        elif "完成" in msg:
            pct = 100

        with _lock:
            st = _jobs.get(job_id)
            if not st:
                return
            st.message = msg
            if pct is not None:
                st.percent = min(100, max(st.percent, pct))

    return cb


def run_build_task(
    *,
    job_id: str,
    api_key: str,
    novel_path: str,
    character_name: str,
    aliases_text: str,
    chat_rules: str,
    model: str,
) -> None:
    _update_job(job_id, character_name=character_name)
    cb = make_status_cb(job_id)
    try:
        text = generate_hidden_system_prompt(
            api_key=api_key,
            novel_path=novel_path,
            character_name=character_name,
            aliases_text=aliases_text,
            chat_rules=chat_rules,
            status_cb=cb,
            model=model,
        )
        _update_job(
            job_id,
            system_prompt=text,
            done=True,
            percent=100,
            message="完成。",
            error=None,
        )
    except Exception as e:
        _update_job(
            job_id,
            done=True,
            error=str(e),
            message=str(e),
        )
