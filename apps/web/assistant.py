"""跨来源问答的装配（批次 18a）：文档候选来源。规格见 docs/spec-32-cross-qa.md。"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from core.app.cross_qa import DocCandidate
from core.app.registry import DocumentRegistry
from core.app.retrieval import resolve_hits
from core.domain.cross_qa import period_label
from core.domain.qa import strip_breadcrumb


class RegistryDocuments:
    """DocumentSource：indexer（已按账号过滤的检索引擎，如 FilteringIndexer）按 group_tags 检索 → 用登记处的分块目录回填
    章节与页码 → 用 registry.records 取命中文档的标题与时间段（period_label）。正文去掉面包屑行。"""

    def __init__(self, indexer: Any, group_tags: Sequence[str], registry: DocumentRegistry) -> None:
        self._indexer, self._tags, self._registry = indexer, list(group_tags), registry

    def search(self, text: str, *, limit: int) -> list[DocCandidate]:
        catalog, titles, _ = self._registry.context()
        hits = resolve_hits(self._indexer.search(text, group_tags=self._tags, top_k=limit), catalog)
        records = self._registry.records({hit.version_id for hit in hits})
        return [DocCandidate(hit.version_id, hit.chunk_id, titles.get(hit.version_id, hit.version_id), hit.section_path,
                             hit.page_start, hit.page_end, period_label((records.get(hit.version_id) or {}).get("period")),
                             strip_breadcrumb(hit.text), hit.score) for hit in hits]
