"""小安对话记录的存储端口（批次 19）。规格见 docs/spec-35-conversation.md。"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any, Protocol, runtime_checkable

from core.domain.conversation import Conversation, ConversationSummary


@runtime_checkable
class ConversationRepo(Protocol):
    """对话记录。所有时间为 ISO 8601 字符串，由调用方给出。"""

    def create(self, conversation_id: str, owner: str, title: str, at: str) -> ConversationSummary:
        """新建空对话（created_at = updated_at = at，turns = 0）。ID 已存在 → ValueError。"""

    def get(self, conversation_id: str) -> Conversation | None:
        """完整对话（各轮按顺序，字典为副本）；不存在 → None。"""

    def summaries(self, owner: str, *, limit: int = 50, offset: int = 0) -> list[ConversationSummary]:
        """owner 的对话，按 (updated_at 降序, conversation_id 降序)，取 [offset, offset + limit)。"""

    def count(self, owner: str) -> int: ...

    def append(self, conversation_id: str, turn: Mapping[str, Any], at: str) -> ConversationSummary:
        """在末尾追加一轮（保存 turn 的副本），updated_at = at，turns 加一，返回更新后的摘要。不存在 → KeyError。"""

    def rename(self, conversation_id: str, title: str, at: str) -> ConversationSummary:
        """改标题，updated_at = at。不存在 → KeyError。"""

    def delete(self, conversation_id: str) -> bool:
        """删除对话及其全部轮次；不存在 → False。"""

    def scrub_versions(self, version_ids: Collection[str]) -> int:
        """批次 20b：所有对话中，资料或引文涉及 version_ids 的轮次按 core.domain.purge.scrub_turn 抹去回答（原位替换），
        返回抹去的轮数。对话的摘要（标题、时间、轮数）不变。已抹去的轮次没有资料，不会再被计入。"""
