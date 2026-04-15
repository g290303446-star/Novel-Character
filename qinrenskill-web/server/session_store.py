"""
服务端会话：仅存内存，用于持有「隐藏扮演指令」与对话历史。
说明（给非技术同事）：就像服务器上的一把临时钥匙串，关掉服务或过期就清空，不写入数据库。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ChatSession:
    hidden_system_prompt: str
    api_key: str
    chat_model: str = "deepseek-chat"
    messages: List[Dict[str, str]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0


class SessionStore:
    def __init__(self, ttl_seconds: int = 86400) -> None:
        self.ttl_seconds = ttl_seconds
        self._data: Dict[str, ChatSession] = {}

    def _purge_expired(self) -> None:
        now = time.time()
        dead = [k for k, v in self._data.items() if v.expires_at <= now]
        for k in dead:
            del self._data[k]

    def create(self, *, hidden_system_prompt: str, api_key: str, chat_model: str = "deepseek-chat") -> str:
        self._purge_expired()
        sid = secrets.token_urlsafe(32)
        now = time.time()
        self._data[sid] = ChatSession(
            hidden_system_prompt=hidden_system_prompt,
            api_key=api_key,
            chat_model=chat_model,
            messages=[],
            created_at=now,
            expires_at=now + self.ttl_seconds,
        )
        return sid

    def get(self, session_id: str) -> Optional[ChatSession]:
        self._purge_expired()
        s = self._data.get(session_id)
        if not s or s.expires_at <= time.time():
            if s and session_id in self._data:
                del self._data[session_id]
            return None
        return s

    def delete(self, session_id: str) -> None:
        self._data.pop(session_id, None)
