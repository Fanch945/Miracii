from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from .keys import get_dsapi_zero

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
# V4.1 Flash: native vision. Old names deepseek-v4-flash / vision-exp still route here.
MODEL = "deepseek-flash"


class LlmError(RuntimeError):
    pass


def _client_key() -> str:
    return get_dsapi_zero().secret


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_client_key()}",
        "Content-Type": "application/json",
    }


def _payload(messages: list[dict], *, stream: bool, max_tokens: int, temperature: float) -> dict:
    return {
        "model": MODEL,
        "messages": messages,
        "stream": stream,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "thinking": {"type": "disabled"},
    }


async def stream_reply(messages: list[dict]) -> AsyncIterator[str]:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=20.0)) as client:
            async with client.stream(
                "POST",
                DEEPSEEK_URL,
                headers=_headers(),
                json=_payload(messages, stream=True, max_tokens=512, temperature=0.92),
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    raise LlmError(_explain_http_error(response.status_code, body))
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        yield text
    except httpx.HTTPError as exc:
        raise LlmError(f"无法连接 DeepSeek：{exc}") from exc


def complete_reply(messages: list[dict], *, max_tokens: int = 280, temperature: float = 0.92) -> str:
    """Blocking one-shot completion for supervisor-initiated speech."""
    try:
        with httpx.Client(timeout=45.0) as client:
            res = client.post(
                DEEPSEEK_URL,
                headers=_headers(),
                json=_payload(messages, stream=False, max_tokens=max_tokens, temperature=temperature),
            )
            if res.status_code >= 400:
                raise LlmError(_explain_http_error(res.status_code, res.text))
            text = res.json()["choices"][0]["message"]["content"]
    except httpx.HTTPError as exc:
        raise LlmError(f"无法连接 DeepSeek：{exc}") from exc
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmError("模型没有返回文本") from exc
    return (text or "").strip()


def _explain_http_error(status: int, body: str) -> str:
    lowered = body.lower()
    if status == 401:
        return "dsapi Zero 密钥无效，请检查 APIkeys.txt"
    if status == 402 or "insufficient balance" in lowered:
        return "DeepSeek 账户余额不足，请到 https://platform.deepseek.com 充值后再试"
    if status == 429:
        return "请求太快，稍等再发"
    if "does not support image" in lowered:
        return "当前模型不接受图片。把说话模型改成 deepseek-flash 后再试"
    return f"DeepSeek HTTP {status}: {body[:400]}"
