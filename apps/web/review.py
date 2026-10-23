"""审核决定的文件存储，以及叠加决定后的知识数据视图。规格：docs/spec-21-review-web.md"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path
from threading import RLock
from typing import Any

from apps.web.knowledge import KnowledgeStore
from apps.web.store import _write_json
from core.domain.knowledge import EntityRecord, EventRecord, MentionRecord
from core.domain.review import (Decision, decision_number, effective_entities, effective_events, effective_mentions,
                                merge_map)


class DecisionStore:
    """数据目录下的 knowledge/decisions.json：{"next": 下一个序号, "decisions": [Decision.to_json…按编号升序]}。
    文件不存在时视为 {"next": 1, "decisions": []}。写入为原子替换，读写在同一把锁内。"""

    def __init__(self, root: str | Path) -> None:
        self._path = Path(root) / "knowledge" / "decisions.json"
        self._lock = RLock()
        data = json.loads(self._path.read_text(encoding="utf-8")) if self._path.exists() else {"next": 1, "decisions": []}
        self._next = data["next"]
        self._decisions = [Decision.from_json(row) for row in data["decisions"]]

    def decisions(self) -> list[Decision]:
        """全部决定，按编号升序。"""
        with self._lock:
            return sorted(self._decisions, key=lambda row: decision_number(row.decision_id))

    def create(self, factory: Callable[[int], Decision]) -> Decision:
        """在锁内调用 factory(序号) 生成决定并保存，返回该决定。
        - factory 抛出的异常原样向上抛出，不写文件、序号不变。
        - 保存时去掉 (group, target) 相同的旧决定，追加新决定，序号加 1（序号只增不减，删除决定后也不复用）。"""
        with self._lock:
            decision = factory(self._next)
            if decision_number(decision.decision_id) != self._next:
                raise ValueError("决定编号必须与分配的序号一致")
            decisions = [row for row in self._decisions if (row.group, row.target) != (decision.group, decision.target)]
            self._save(self._next + 1, [*decisions, decision])
            return decision

    def remove(self, decision_id: str) -> Decision | None:
        """删除（撤销）一条决定，返回被删除的决定；不存在返回 None（不写文件）。"""
        with self._lock:
            decision = next((row for row in self._decisions if row.decision_id == decision_id), None)
            if decision is not None:
                self._save(self._next, [row for row in self._decisions if row.decision_id != decision_id])
            return decision

    def rewrite(self, decisions: Iterable[Decision]) -> None:
        with self._lock:
            current = {row.decision_id: row for row in self._decisions}
            updates = list(decisions)
            for row in updates:
                old = current.get(row.decision_id)
                if old is None or (old.kind, old.target) != (row.kind, row.target):
                    raise KeyError(row.decision_id)
            if updates:
                current.update((row.decision_id, row) for row in updates)
                self._save(self._next, list(current.values()))

    def _save(self, next_number: int, decisions: list[Decision]) -> None:
        """按编号升序原子写入并更新内存状态（内部方法，可自行调整）。"""
        with self._lock:
            ordered = sorted(decisions, key=lambda row: decision_number(row.decision_id))
            _write_json(self._path, {"next": next_number, "decisions": [row.to_json() for row in ordered]})
            self._next, self._decisions = next_number, ordered


class ReviewedKnowledge:
    """KnowledgeStore 的只读包装：entities/mentions/events 返回叠加审核决定后的有效视图，其余方法原样转发。
    抽取作业必须使用原始 KnowledgeStore（新实体对齐依赖原始实体），视图与连线分析使用本类。"""

    def __init__(self, knowledge: KnowledgeStore, decisions: DecisionStore) -> None:
        self._knowledge = knowledge
        self._decisions = decisions

    def merge_map(self) -> dict[str, str]:
        """merge_map(原始实体, 全部决定)。"""
        return merge_map(self._knowledge.entities(), self._decisions.decisions())

    def entities(self) -> list[EntityRecord]:
        """effective_entities(原始实体, 全部决定)。"""
        return effective_entities(self._knowledge.entities(), self._decisions.decisions())

    def mentions(self) -> list[MentionRecord]:
        """effective_mentions(原始提及, merge_map())。"""
        return effective_mentions(self._knowledge.mentions(), self.merge_map())

    def events(self) -> list[EventRecord]:
        """effective_events(原始事件, 全部决定, merge_map())。"""
        decisions = self._decisions.decisions()
        return effective_events(self._knowledge.events(), decisions, merge_map(self._knowledge.entities(), decisions))

    def extracted(self) -> set[str]:
        """原样转发。"""
        return self._knowledge.extracted()

    def document(self, version_id: str) -> dict[str, Any] | None:
        """原样转发。"""
        return self._knowledge.document(version_id)
