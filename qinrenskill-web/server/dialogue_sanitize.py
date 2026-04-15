"""
对角色扮演模型的单次回复做「只保留台词」的二次 API 清洗。
不修改用户会话中的扮演 system prompt。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.deepseek_client import DeepSeekClient

SANITIZE_SYSTEM = """你是纯文本后处理器。用户会给你一段「角色扮演模型的回复」，其中可能混有：
- 用 *星号* 或 （圆括号）或【方括号】写的动作、神态、心理、旁白；
- 第三人称叙述（如「他低声说」「她笑了笑」）；
- 舞台说明或小说式描写。

你的任务：只输出角色**直接说出口的话**（对白本身），用于语音朗读或纯对话展示。
规则：
1. 删除所有动作、神态、心理、环境描写；删除 *…*、（…）、【…】中的说明性内容。
2. 若一句里混有对白与旁白，只保留对白部分；多条对白可保留为连续段落，用换行分隔。
3. 不要添加引号包裹、不要写「以下是台词」等元话语；不要解释你做了什么。
4. 若几乎全是旁白、无法分离出明确对白，则输出与原文核心语义最接近的简短口语化句子，不要留空。
5. 输出语言与原文主要语言一致（多为中文则输出中文）。"""


def sanitize_spoken_dialogue(
    client: DeepSeekClient,
    text: str,
    *,
    model: str,
    max_tokens: int = 1800,
) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    resp = client.chat_completions(
        model=model,
        system=SANITIZE_SYSTEM,
        user=raw,
        max_tokens=max_tokens,
        temperature=0.1,
    )
    return (resp.content or "").strip()
