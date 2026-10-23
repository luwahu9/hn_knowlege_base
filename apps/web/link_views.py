"""引用与冲突视图的计算与缓存（批次 18c）。规格见 docs/spec-34-link-views.md。"""
from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from threading import Lock
from typing import Any

from core.app.knowledge import DecisionRepo, KnowledgeQueries, LinkRepo
from core.app.link_targets import LinkTargets
from core.app.registry import DocumentRegistry
from core.domain.link_targets import build_links_from
from core.domain.permissions import Visibility
from core.domain.review import apply_conflict_decisions, apply_reference_decisions

MAX_AGE = 30.0          # 秒：同一事件序号下缓存的最长保留时间（防止有写入没有追加事件时一直读旧值）
MAX_SCOPES = 32         # 缓存的可读范围个数（按最近使用淘汰）


class LinkViews:
    """view(scope) → {"links","pending","unresolved","conflicts"}，语义与批次 16b 的 link_views 相同：
    给出 scope 时只用可读的已发布文档，只取来源文档可读的引用（目标只在可读文档中解析），冲突只剩两边都可读的。

    缓存（批次 18c）：
    - 输入（已发布文档、全部引用、物化结果）按 store.last_event_seq() 缓存：序号不变且未超过 max_age 秒时不重新读取，
      序号在读取输入之前取得（读取期间有新写入时，下一次请求会因序号变化而重读）；
    - 视图按 (序号, scope) 缓存，最多 max_scopes 个；
    - 返回的字典与列表由多个请求共用，调用方不得修改。
    能使视图变化的写入都会追加事件：文档发布（document_published）、引用提取（links_done）、冲突判断（conflicts_judged）、
    审核决定（decision_changed）。"""

    def __init__(self, store: DocumentRegistry, links: LinkRepo, decisions: DecisionRepo, queries: KnowledgeQueries,
                 targets: LinkTargets, *, max_age: float = MAX_AGE, max_scopes: int = MAX_SCOPES,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._store, self._links, self._decisions, self._queries, self._targets = store, links, decisions, queries, targets
        self._max_age, self._max_scopes, self._clock = max_age, max_scopes, clock
        self._lock = Lock()
        self._inputs: tuple[int, float, dict[str, Any]] | None = None
        self._views: OrderedDict[tuple[int, Visibility | None], tuple[float, dict[str, Any]]] = OrderedDict()

    def _load(self, seq: int, now: float) -> dict[str, Any]:
        if self._inputs is not None and self._inputs[0] == seq and now - self._inputs[1] < self._max_age:
            return self._inputs[2]
        rows = [row for row in self._store.documents() if row["status"] == "published"]
        references = self._links.references()
        published = {row["version_id"]: row["title"] for row in rows}
        inputs = {"rows": rows, "published": published, "references": references,
                  "targets": self._targets.current(published, references).targets}
        self._inputs = (seq, now, inputs)
        self._views.clear()
        return inputs

    def view(self, scope: Visibility | None = None) -> dict[str, Any]:
        with self._lock:
            seq, now = self._store.last_event_seq(), self._clock()
            inputs = self._load(seq, now)
            key = (seq, scope)
            cached = self._views.get(key)
            if cached is not None and now - cached[0] < self._max_age:
                self._views.move_to_end(key)
                return cached[1]
            documents = {row["version_id"]: row["title"] for row in inputs["rows"]
                         if scope is None or scope.contains(row["version_id"], row.get("folder", ""))}
            references = [ref for ref in inputs["references"] if scope is None or ref.version_id in documents]
            decisions = self._decisions.decisions()
            value = {**apply_reference_decisions(build_links_from(references, documents, inputs["targets"]), decisions,
                                                 documents),
                     "conflicts": apply_conflict_decisions(self._queries.conflict_rows(scope=scope), decisions)}
            self._views[key] = (now, value)
            while len(self._views) > self._max_scopes:
                self._views.popitem(last=False)
            return value
