"""文档的撤下、恢复与版本确认（批次 20a）。规格见 docs/spec-36-document-lifecycle.md。"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.app.permissions import IndexTagger
from core.app.ports import Chunk, Indexer, TransientError
from core.app.registry import DocumentRegistry
from core.domain.lifecycle import (MANUAL, PUBLISHED, REPLACED, SOURCE_MISSING, SUPERSEDED, WITHDRAWN, lifecycle_entry,
                                   same_document, withdrawn_for_missing)


class LifecycleService:
    """顺序约定：先改检索引擎，再改登记处。引擎操作失败时登记处不变；登记处写入失败时重做同一操作即可修复
    （撤下对引擎是幂等的，恢复会先删后建）。

    - withdraw(version_id, by, reason)：已发布 → 已撤下。先 indexer.remove，再 registry.set_status。
      已经是已撤下或已替代时只再做一次 indexer.remove（修复中途失败），返回原记录，不追加 lifecycle。
      其他状态 → ValueError；不存在 → KeyError。
    - restore(version_id, by)：已撤下或已替代 → 已发布。分块取 registry.stored_chunks（没有 → ValueError），
      标签取 tagger.tags(version_id, folder)，indexer.publish 后 indexer.verify；verify 的 indexed 为假 → TransientError
      （登记处不变）；成功后 set_status(published, chunk_map=回执的分块映射, engine_doc_id=回执的文档 ID)，
      再 tagger.record（失败忽略，下次标签同步会补上）。返回更新后的记录。
    - keep(version_id, by)：确认保留一个已发布版本：同一文档的其他已发布版本逐个 indexer.remove 再
      set_status(superseded, superseded_by=version_id)，只处理 allowed(版本号) 为真的（接口传入当前账号的可读判断）。
      version_id 不是已发布 → ValueError。返回被替代的版本号列表。
    - withdraw_missing(version_id)：目录扫描发现源文件缺失。仅当记录为已发布、且 origin 为 "scan" 时撤下
      （by "scan"，reason SOURCE_MISSING），返回 True；否则什么都不做，返回 False。
    - restore_reappeared(version_id)：源文件以相同内容再出现。仅当记录因源文件缺失而被撤下（withdrawn_for_missing）时
      恢复（by "scan"），返回 True；否则返回 False。"""

    def __init__(self, registry: DocumentRegistry, indexer: Indexer, tagger: IndexTagger,
                 clock: Callable[[], str]) -> None:
        self._registry, self._indexer, self._tagger, self._clock = registry, indexer, tagger, clock

    def _record(self, version_id: str) -> dict[str, Any]:
        detail = self._registry.detail(version_id)
        if detail is None:
            raise KeyError(version_id)
        return detail["document"]

    def withdraw(self, version_id: str, *, by: str, reason: str = "") -> dict[str, Any]:
        record = self._record(version_id)
        if record["status"] in (WITHDRAWN, SUPERSEDED):
            self._indexer.remove(version_id)
            return record
        if record["status"] != PUBLISHED:
            raise ValueError("只能撤下已发布的文档")
        entry = lifecycle_entry("withdraw", at=self._clock(), by=by, reason=reason or MANUAL)
        self._indexer.remove(version_id)
        return self._registry.set_status(version_id, WITHDRAWN, entry)

    def restore(self, version_id: str, *, by: str) -> dict[str, Any]:
        record = self._record(version_id)
        if record["status"] not in (WITHDRAWN, SUPERSEDED):
            raise ValueError("只能恢复已撤下或已替代的文档")
        rows = self._registry.stored_chunks(version_id)
        if not rows:
            raise ValueError("没有保存的分块，无法恢复，请重新导入原件")
        chunks = [Chunk(row["chunk_id"], row["text"], row["page_start"], row["page_end"], row["section_path"],
                        row["meta"]) for row in rows]
        tags = tuple(self._tagger.tags(version_id, record.get("folder", "")))
        receipt = self._indexer.publish(version_id, chunks, list(tags))
        if not self._indexer.verify(version_id).indexed:
            raise TransientError("重新写入检索引擎后核对未通过，请稍后重试")
        updated = self._registry.set_status(version_id, PUBLISHED, lifecycle_entry("restore", at=self._clock(), by=by),
                                            chunk_map=receipt.chunk_map, engine_doc_id=receipt.engine_doc_id)
        try:
            self._tagger.record(version_id, tags)
        except Exception:
            pass
        return updated

    def keep(self, version_id: str, *, by: str, allowed: Callable[[str], bool] = lambda version: True) -> list[str]:
        record = self._record(version_id)
        if record["status"] != PUBLISHED:
            raise ValueError("只能保留已发布的版本")
        others = [row["version_id"] for row in same_document(self._registry.documents(), record)
                  if row["status"] == PUBLISHED and row["version_id"] != version_id and allowed(row["version_id"])]
        for other in others:
            self._indexer.remove(other)
            self._registry.set_status(other, SUPERSEDED, lifecycle_entry(
                "supersede", at=self._clock(), by=by, reason=REPLACED, superseded_by=version_id))
        return others

    def withdraw_missing(self, version_id: str) -> bool:
        try:
            record = self._record(version_id)
        except KeyError:
            return False
        if record["status"] != PUBLISHED or record.get("origin") != "scan":
            return False
        self.withdraw(version_id, by="scan", reason=SOURCE_MISSING)
        return True

    def restore_reappeared(self, version_id: str) -> bool:
        try:
            record = self._record(version_id)
        except KeyError:
            return False
        if not withdrawn_for_missing(record):
            return False
        self.restore(version_id, by="scan")
        return True
