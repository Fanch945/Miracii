from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from .keys import get_dsapi_zero

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-flash"


class LlmError(RuntimeError):
    pass


def _client_key() -> str:
    return get_dsapi_zero().secret


async def stream_reply(messages: list[dict]) -> AsyncIterator[str]:
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": True,
        "temperature": 0.92,
        "max_tokens": 512,
        "thinking": {"type": "disabled"},
    }
    headers = {
        "Authorization": f"Bearer {_client_key()}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=20.0)) as client:
            async with client.stream("POST", DEEPSEEK_URL, headers=headers, json=payload) as response:
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


def _explain_http_error(status: int, body: str) -> str:
    lowered = body.lower()
    if status == 401:
        return "dsapi Zero 密钥无效，请检查 APIkeys.txt"
    if status == 402 or "insufficient balance" in lowered:
        return "DeepSeek 账户余额不足，请到 https://platform.deepseek.com 充值后再试"
    if status == 429:
        return "请求太快，稍等再发"
    return f"DeepSeek HTTP {status}: {body[:400]}"
