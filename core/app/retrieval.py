"""检索命中回填。规格：docs/spec-12-e2e-retrieval.md

适配器返回的 Hit 里 chunk_id 是引擎分块 ID、页码和章节为 None。
用发布时得到的 IndexReceipt.chunk_map 和分块本身，把命中映射回我们的分块。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Protocol

from core.app.ports import Chunk, Hit, IndexReceipt


class ChunkLookup(Protocol):
    """按 (version_id, 引擎分块 ID) 查我们的 Chunk（批次 15b：数据库实现按需查询，不再整体读入内存）。"""

    def lookup(self, version_id: str, engine_chunk_id: str) -> Chunk | None: ...


class ChunkCatalog:
    """(version_id, 引擎分块 ID) → 我们的 Chunk。"""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], Chunk] = {}

    def add(self, receipt: IndexReceipt, chunks: Iterable[Chunk]) -> None:
        """登记一次发布。chunk_map 中出现但 chunks 里没有的 chunk_id → ValueError；
        chunks 里有但 chunk_map 中没有的分块不登记。同一版本再次登记时，先清除该版本的旧条目。
        抛出 ValueError 时目录保持不变。"""
        by_id = {chunk.chunk_id: chunk for chunk in chunks}
        if any(chunk_id not in by_id for chunk_id in receipt.chunk_map):
            raise ValueError("回执包含未提供的分块")
        updated = {key: value for key, value in self._entries.items()
                   if key[0] != receipt.version_id}
        updated.update({(receipt.version_id, engine_id): by_id[chunk_id]
                        for chunk_id, engine_id in receipt.chunk_map.items()})
        self._entries = updated

    def lookup(self, version_id: str, engine_chunk_id: str) -> Chunk | None:
        return self._entries.get((version_id, engine_chunk_id))

    def __len__(self) -> int:
        """已登记的 (version_id, 引擎分块 ID) 条目数。"""
        return len(self._entries)


def resolve_hits(hits: Sequence[Hit], catalog: ChunkLookup) -> list[Hit]:
    """逐个回填：找到对应分块时，chunk_id 换成我们的 chunk_id，补上 page_start、page_end、section_path，
    其余字段（version_id、text、score）不变；找不到时原样保留。顺序不变。"""
    result: list[Hit] = []
    for hit in hits:
        chunk = catalog.lookup(hit.version_id, hit.chunk_id)
        result.append(hit if chunk is None else replace(
            hit, chunk_id=chunk.chunk_id, page_start=chunk.page_start,
            page_end=chunk.page_end, section_path=chunk.section_path,
        ))
    return result
