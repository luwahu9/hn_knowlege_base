"""引用目标的同步（批次 18b）。规格见 docs/spec-33-link-targets.md。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from threading import Lock

from core.app.knowledge import LinkRepo
from core.domain.link_targets import TargetState, plan_sync
from core.domain.relations import ReferenceRecord


class LinkTargets:
    """current(published, references)：返回与 published（{version_id: 标题}，全部已发布文档）和 references（全部引用记录）
    一致的物化结果。

    - 进程内保留上次读到的结果，links.link_targets_version() 不变时不重新读取存储；
    - 先用这份结果计算 plan_sync；为空 → 直接返回（没有任何写入）；
    - 否则 links.sync_link_targets 在锁内对最新结果重新计算并写入，返回写入后的结果。
    多个进程同时调用时结果仍正确：写入总在锁内基于最新结果计算；某个进程的 published 稍旧时，下一次调用会补上差异。"""

    def __init__(self, links: LinkRepo) -> None:
        self._links = links
        self._lock = Lock()
        self._state: TargetState | None = None

    def current(self, published: Mapping[str, str], references: Sequence[ReferenceRecord]) -> TargetState:
        rows = [(record.ref_id, record.version_id, record.title) for record in references]
        with self._lock:
            state = self._state
            if state is None or state.version != self._links.link_targets_version():
                state = self._links.link_targets()
            if not plan_sync(state, published, rows).empty:
                state = self._links.sync_link_targets(lambda fresh: plan_sync(fresh, published, rows))
            self._state = state
            return state
