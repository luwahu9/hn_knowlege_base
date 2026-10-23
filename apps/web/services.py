"""Web 依赖装配及敏感信息过滤。"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.app.ports import BlobStore, Converter, Indexer, LlmGateway
from apps.runtime import CONFIG_HASH


@dataclass(frozen=True)
class Services:
    blobs: BlobStore
    converter: Converter
    indexer: Indexer
    llm: LlmGateway
    config_hash: str = CONFIG_HASH
    indexer_for: Callable[[str], Indexer] | None = None    # 批次 21b：按数据集 ID 取检索引擎（重建与彻底删除用）


class Redactor:
    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        self._secrets = tuple(sorted({key for key in secrets if key}, key=len, reverse=True))

    def text(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, "<redacted>")
        return value

    def clean(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(str(key)): self.clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(item) for item in value]
        return value

    def error(self, exc: Exception) -> str:
        return self.text(f"{type(exc).__name__}: {exc}")
