import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class DeepSeekResponse:
    content: str
    raw: Dict[str, Any]


class DeepSeekError(RuntimeError):
    pass


def _post_json(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout_s: int,
) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body)


class DeepSeekClient:
    """
    DeepSeek Chat Completions 最小封装。
    说明（给非技术同事）：这里只做“发请求、拿回复、遇到限流/网络抖动自动重试”，不做任何业务逻辑。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        timeout_s: int = 120,
        max_retries: int = 4,
        backoff_s: float = 1.8,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s

        if not self.api_key:
            raise ValueError("API Key 不能为空。")

    def chat_completions(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2000,
        temperature: float = 0.2,
    ) -> DeepSeekResponse:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        last_err: Optional[BaseException] = None
        for attempt in range(self.max_retries + 1):
            try:
                raw = _post_json(url, headers, payload, timeout_s=self.timeout_s)
                msg = raw.get("choices", [{}])[0].get("message", {}) or {}
                content = msg.get("content", "")
                # deepseek-reasoner 有时把主要输出放在 reasoning_content
                reasoning_content = msg.get("reasoning_content", "")

                parts: List[str] = []
                if isinstance(content, str) and content.strip():
                    parts.append(content.strip())
                if isinstance(reasoning_content, str) and reasoning_content.strip():
                    parts.append(reasoning_content.strip())

                merged = "\n\n".join(parts).strip()
                if not merged:
                    raise DeepSeekError(f"API 返回空内容：{raw!r}")
                return DeepSeekResponse(content=merged, raw=raw)
            except urllib.error.HTTPError as e:
                last_err = e
                code = getattr(e, "code", None)
                # 429/5xx 走重试；其余直接报错
                if code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep((self.backoff_s ** attempt) + 0.2)
                    continue
                try:
                    body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    body = ""
                raise DeepSeekError(f"HTTPError {code}: {body}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                last_err = e
                if attempt < self.max_retries:
                    time.sleep((self.backoff_s ** attempt) + 0.2)
                    continue
                raise DeepSeekError(f"网络错误：{e}") from e
            except json.JSONDecodeError as e:
                last_err = e
                if attempt < self.max_retries:
                    time.sleep((self.backoff_s ** attempt) + 0.2)
                    continue
                raise DeepSeekError("API 返回的 JSON 解析失败。") from e

        raise DeepSeekError(f"请求失败：{last_err}")  # pragma: no cover

    def chat_multi_turn(
        self,
        *,
        model: str,
        system: str,
        messages: List[Dict[str, str]],
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> DeepSeekResponse:
        """
        多轮对话：messages 为不含 system 的 user/assistant 列表。
        """
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}] + list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        last_err: Optional[BaseException] = None
        for attempt in range(self.max_retries + 1):
            try:
                raw = _post_json(url, headers, payload, timeout_s=self.timeout_s)
                msg = raw.get("choices", [{}])[0].get("message", {}) or {}
                content = msg.get("content", "")
                reasoning_content = msg.get("reasoning_content", "")
                parts: List[str] = []
                if isinstance(content, str) and content.strip():
                    parts.append(content.strip())
                if isinstance(reasoning_content, str) and reasoning_content.strip():
                    parts.append(reasoning_content.strip())
                merged = "\n\n".join(parts).strip()
                if not merged:
                    raise DeepSeekError(f"API 返回空内容：{raw!r}")
                return DeepSeekResponse(content=merged, raw=raw)
            except urllib.error.HTTPError as e:
                last_err = e
                code = getattr(e, "code", None)
                if code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep((self.backoff_s ** attempt) + 0.2)
                    continue
                try:
                    body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    body = ""
                raise DeepSeekError(f"HTTPError {code}: {body}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                last_err = e
                if attempt < self.max_retries:
                    time.sleep((self.backoff_s ** attempt) + 0.2)
                    continue
                raise DeepSeekError(f"网络错误：{e}") from e
            except json.JSONDecodeError as e:
                last_err = e
                if attempt < self.max_retries:
                    time.sleep((self.backoff_s ** attempt) + 0.2)
                    continue
                raise DeepSeekError("API 返回的 JSON 解析失败。") from e

        raise DeepSeekError(f"请求失败：{last_err}")  # pragma: no cover

