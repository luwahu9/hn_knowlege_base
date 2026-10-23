"""小安对话记录的文件存储（批次 19）。规格见 docs/spec-35-conversation.md。

数据目录下 conversations/<conversation_id>.json：{"summary": {...}, "turns": [...]}，原子替换写入。
启动时全部读入内存（JSON 模式只用于单进程、小规模）；写入先写文件再改内存。"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, replace
from pathlib import Path
from threading import RLock
from typing import Any

from apps.web.store import _write_json
from core.domain.conversation import Conversation, ConversationSummary, valid_conversation_id
from core.domain.purge import scrub_turn


class ConversationStore:
    def __init__(self, root: str | Path) -> None:
        self._dir = Path(root) / "conversations"
        self._lock = RLock()
        self._rows: dict[str, tuple[ConversationSummary, list[dict[str, Any]]]] = {}
        for path in sorted(self._dir.glob("c_*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            self._rows[data["summary"]["conversation_id"]] = (ConversationSummary(**data["summary"]), data["turns"])

    def _save(self, summary: ConversationSummary, turns: list[dict[str, Any]]) -> None:
        if not valid_conversation_id(summary.conversation_id):
            raise ValueError("对话 ID 格式无效")
        _write_json(self._dir / f"{summary.conversation_id}.json", {"summary": asdict(summary), "turns": turns})
        self._rows[summary.conversation_id] = (summary, turns)

    def create(self, conversation_id, owner, title, at):
        with self._lock:
            if conversation_id in self._rows:
                raise ValueError("对话已存在")
            summary = ConversationSummary(conversation_id, owner, title, at, at, 0)
            self._save(summary, [])
            return summary

    def get(self, conversation_id):
        with self._lock:
            row = self._rows.get(conversation_id)
            return None if row is None else Conversation(row[0], tuple(copy.deepcopy(row[1])))

    def summaries(self, owner, *, limit=50, offset=0):
        with self._lock:
            rows = sorted((row[0] for row in self._rows.values() if row[0].owner == owner),
                          key=lambda s: (s.updated_at, s.conversation_id), reverse=True)
            return rows[offset:offset + limit]

    def count(self, owner):
        with self._lock:
            return sum(row[0].owner == owner for row in self._rows.values())

    def append(self, conversation_id, turn, at):
        with self._lock:
            summary, turns = self._rows[conversation_id]
            turns = [*turns, copy.deepcopy(dict(turn))]
            summary = replace(summary, updated_at=at, turns=len(turns))
            self._save(summary, turns)
            return summary

    def rename(self, conversation_id, title, at):
        with self._lock:
            summary, turns = self._rows[conversation_id]
            summary = replace(summary, title=title, updated_at=at)
            self._save(summary, turns)
            return summary

    def scrub_versions(self, version_ids):
        count = 0
        with self._lock:
            for summary, turns in list(self._rows.values()):
                updated = [scrub_turn(turn, version_ids) for turn in turns]
                if any(row is not None for row in updated):
                    count += sum(row is not None for row in updated)
                    self._save(summary, [new if new is not None else old for old, new in zip(turns, updated)])
            return count

    def delete(self, conversation_id):
        with self._lock:
            if self._rows.pop(conversation_id, None) is None:
                return False
            (self._dir / f"{conversation_id}.json").unlink(missing_ok=True)
            return True
