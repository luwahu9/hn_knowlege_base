"""彻底删除（批次 20b）。规格见 docs/spec-37-purge.md。"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from core.app.conversation import ConversationRepo
from core.app.knowledge import DecisionRepo, KnowledgeRepo, LinkRepo
from core.app.ports import BlobStore, Indexer
from core.app.registry import DocumentRegistry
from core.domain.purge import (PURGEABLE, PURGED, SCRUBBED_LABEL, blob_prefixes, decision_related, purge_entry,
                               scrub_decision, scrubbed)


class PurgeService:
    """purge(version_id, by) → 删除报告。只能删除已撤下或已替代的文档（其他状态 → ValueError，不存在 → KeyError）。

    依次执行，每一步都可以重做；登记处最后改为墓碑，作为提交点。中途任何一步失败，文档仍为已撤下或已替代，
    再次调用即可从头重做（已完成的步骤第二次什么也不做）：
    1. indexer.remove（撤下时已删过，这里再删一次，幂等）；再调用 forget(version_id)（批次 21b：从重建中、
       保留待回退的检索数据集中移除，见 core.app.reindex.forget_everywhere）；
    2. conversations.scrub_versions({version_id})：对话中引用它的回答抹去；
    3. links.purge_document：引用提取结果与相关冲突判断；
    4. knowledge.purge_document(version_id, merge_map())：事件、提及，以及无人提及的实体的名称；
    5. 审核决定：涉及该版本、或涉及名称已抹去的实体的决定（decision_related），抹去引文与说明（scrub_decision；
       已经抹去过的不再写）；
    6. 原件存储：blob_prefixes(记录) 下的全部对象。若还有其他未删除的记录使用同一 sha256（如导入失败后重试留下的记录），
       原件保留，报告中 "blobs_kept" 为真；
    7. registry.purge：分块删除，记录换成墓碑，追加 document_removed 事件（各视图的缓存随之失效）。"""

    def __init__(self, registry: DocumentRegistry, indexer: Indexer, knowledge: KnowledgeRepo, links: LinkRepo,
                 decisions: DecisionRepo, conversations: ConversationRepo, blobs: BlobStore,
                 merge_map: Callable[[], Mapping[str, str]], clock: Callable[[], str], *,
                 forget: Callable[[str], None] = lambda version_id: None) -> None:
        self._registry, self._indexer, self._knowledge, self._links = registry, indexer, knowledge, links
        self._decisions, self._conversations, self._blobs = decisions, conversations, blobs
        self._merge_map, self._clock, self._forget = merge_map, clock, forget

    def purge(self, version_id: str, *, by: str) -> dict[str, Any]:
        detail = self._registry.detail(version_id)
        if detail is None:
            raise KeyError(version_id)
        record = detail["document"]
        if record["status"] == PURGED:
            raise ValueError("该文档已彻底删除")
        if record["status"] not in PURGEABLE:
            raise ValueError("只能彻底删除已撤下或已替代的文档，请先撤下")
        chunks = len(self._registry.stored_chunks(version_id))
        self._indexer.remove(version_id)
        self._forget(version_id)                    # 批次 21b：重建中或保留待回退的数据集里也移除
        turns = self._conversations.scrub_versions({version_id})
        links = self._links.purge_document(version_id)
        knowledge = self._knowledge.purge_document(version_id, self._merge_map())
        gone = {entity.id for entity in self._knowledge.entities() if scrubbed(entity)}
        related = [scrub_decision(d) for d in self._decisions.decisions()
                   if decision_related(d, version_id, gone) and (d.snapshot or d.label != SCRUBBED_LABEL)]
        self._decisions.rewrite(related)
        digest = record.get("sha256")
        shared = any(row["version_id"] != version_id and row["status"] != PURGED and row.get("sha256") == digest
                     for row in self._registry.documents()) if digest else False
        blobs = 0 if shared else sum(self._blobs.delete_prefix(prefix) for prefix in blob_prefixes(record))
        self._registry.purge(version_id, purge_entry(at=self._clock(), by=by))
        return {"version_id": version_id, "chunks": chunks, "events": knowledge["events"],
                "mentions": knowledge["mentions"], "relations": knowledge.get("relations", 0),   # 批次 23b
                "entities_scrubbed": knowledge["entities_scrubbed"],
                "references": links["references"], "conflicts": links["conflicts"],
                "decisions_scrubbed": [d.decision_id for d in related], "conversation_turns": turns,
                "blobs_deleted": blobs, "blobs_kept": shared}
