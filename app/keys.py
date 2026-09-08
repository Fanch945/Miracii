from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEYS_FILE = ROOT / "APIkeys.txt"


@dataclass(frozen=True)
class ApiKey:
    provider: str
    alias: str
    secret: str


def load_keys(path: Path | None = None) -> list[ApiKey]:
    target = path or KEYS_FILE
    if not target.exists():
        raise FileNotFoundError(f"找不到密钥文件：{target}")

    keys: list[ApiKey] = []
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("=", 2)
        if len(parts) != 3:
            raise ValueError(f"密钥行格式应为 provider=alias=secret，收到：{line}")
        provider, alias, secret = (p.strip() for p in parts)
        if not provider or not alias or not secret:
            raise ValueError(f"密钥行有空字段：{line}")
        keys.append(ApiKey(provider=provider, alias=alias, secret=secret))
    return keys


def get_dsapi_zero(path: Path | None = None) -> ApiKey:
    matches = [
        key
        for key in load_keys(path)
        if key.provider.lower() == "dsapi" and "zero" in key.alias.lower()
    ]
    if not matches:
        raise RuntimeError("APIkeys.txt 里没有 dsapi Zero（provider=dsapi 且 alias 含 Zero）")
    return matches[0]
