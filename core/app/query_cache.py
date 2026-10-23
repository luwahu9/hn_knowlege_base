"""查询结果缓存（批次 15b）：只缓存全量统计类查询，以知识事件序号为版本号。规格见 docs/spec-25-knowledge-db.md 第五节。

被缓存的方法：knowledge_counts、conflict_candidate_count、conflict_rows、review_entities、review_events、
entity_graph（仅 focus 为 None 的全图）、entity_names（批次 18a，过期时同步重算，返回同一对象而非深拷贝）。其余方法直接转发。
批次 16b：scope 是缓存键的一部分（同一组用户组的账号共用条目）；条目总数超过 max_entries 时淘汰最久未用的。
缓存条目在以下任一情况过期：revision() 的值变化（有新的知识事件）、超过 max_age 秒。
过期但存在的条目：立即返回旧值，同时在后台重算一次（同一个键同时只有一个重算）；重算完成后替换条目。
没有条目时同步计算。重算出错时保留旧值，下一次调用再试。
返回值是缓存对象的深拷贝，调用方修改不影响缓存。
（批量导入期间知识事件不断增加，全量统计不宜每次都同步重算；界面上的全图与计数允许落后几秒。）
"""
from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable
from datetime import date
from typing import Any

from core.app.knowledge import CANDIDATE_COUNT_CAP, REVIEW_LIMIT
from core.domain.knowledge import DETAIL_LIMIT
from core.domain.permissions import Visibility



def _scoped(scope: Visibility | None) -> dict[str, Any]:
    """只在给出 scope 时才传给内层（不限范围时调用形式与批次 15b 相同）。"""
    return {} if scope is None else {"scope": scope}


class CachedQueries:
    def __init__(self, inner: Any, revision: Callable[[], int], *, max_age: float = 30.0,
                 clock: Callable[[], float] = time.monotonic,
                 background: Callable[[Callable[[], None]], None] | None = None, max_entries: int = 256) -> None:
        """background(任务)：在后台执行重算任务，缺省为启动一个守护线程；测试可传入同步执行或收集起来稍后执行。"""
        self._inner, self._revision, self._max_age, self._clock = inner, revision, max_age, clock
        self._background = background or (lambda task: threading.Thread(target=task, daemon=True).start())
        self._lock = threading.Lock()
        self._entries: dict[tuple[Any, ...], tuple[int, float, Any]] = {}
        self._max_entries = max_entries
        self._refreshing: set[tuple[Any, ...]] = set()

    def _cached(self, key: tuple[Any, ...], compute: Callable[[], Any]) -> Any:
        revision, now = self._revision(), self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries[key] = self._entries.pop(key)          # 记为最近使用
            if entry is not None and entry[0] == revision and now - entry[1] < self._max_age:
                return copy.deepcopy(entry[2])
            if entry is not None:
                if key not in self._refreshing:
                    self._refreshing.add(key)
                    self._background(lambda: self._refresh(key, compute))
                return copy.deepcopy(entry[2])
        value = compute()
        with self._lock:
            self._store(key, (revision, now, value))
        return copy.deepcopy(value)

    def _store(self, key: tuple[Any, ...], entry: tuple[int, float, Any]) -> None:
        self._entries.pop(key, None)
        self._entries[key] = entry
        while len(self._entries) > self._max_entries:
            del self._entries[next(iter(self._entries))]

    def _refresh(self, key: tuple[Any, ...], compute: Callable[[], Any]) -> None:
        try:
            revision, now = self._revision(), self._clock()
            value = compute()
            with self._lock:
                self._store(key, (revision, now, value))
        except Exception:
            pass
        finally:
            with self._lock:
                self._refreshing.discard(key)

    def warm(self) -> None:
        """在后台预先计算界面最常用的全量统计（默认全图与计数），Web 启动时调用，避免第一个请求同步等待。
        出错时忽略（之后的请求照常同步计算）。"""
        def task() -> None:
            for compute in (lambda: self.entity_graph(), self.knowledge_counts):
                try:
                    compute()
                except Exception:
                    pass
        self._background(task)

    def entity_graph(self, *, include_review: bool = True, focus: str | None = None, depth: int = 1,
                     limit: int = 300, scope: Visibility | None = None) -> dict[str, Any]:
        if focus is not None:
            return self._inner.entity_graph(include_review=include_review, focus=focus, depth=depth, limit=limit,
                                            **_scoped(scope))
        return self._cached(("entity_graph", include_review, limit, scope),
                            lambda: self._inner.entity_graph(include_review=include_review, limit=limit, **_scoped(scope)))

    # —— 缓存的方法 ——

    def knowledge_counts(self, *, scope: Visibility | None = None) -> dict[str, Any]:
        return self._cached(("knowledge_counts", scope), lambda: self._inner.knowledge_counts(**_scoped(scope)))

    def conflict_candidate_count(self, cap: int = CANDIDATE_COUNT_CAP) -> int:
        return self._cached(("conflict_candidate_count", cap), lambda: self._inner.conflict_candidate_count(cap))

    def conflict_rows(self, *, scope: Visibility | None = None) -> list[dict[str, Any]]:
        return self._cached(("conflict_rows", scope), lambda: self._inner.conflict_rows(**_scoped(scope)))

    def review_entities(self, limit: int = REVIEW_LIMIT, *, scope: Visibility | None = None) -> list[dict[str, Any]]:
        return self._cached(("review_entities", limit, scope), lambda: self._inner.review_entities(limit, **_scoped(scope)))

    def review_events(self, limit: int = REVIEW_LIMIT, *, scope: Visibility | None = None) -> list[dict[str, Any]]:
        return self._cached(("review_events", limit, scope), lambda: self._inner.review_events(limit, **_scoped(scope)))

    # —— 直接转发 ——

    def merge_map(self) -> dict[str, str]:
        return self._inner.merge_map()

    def entity_detail(self, entity_id: str, *, limit: int = DETAIL_LIMIT,
                      scope: Visibility | None = None) -> dict[str, Any] | None:
        return self._inner.entity_detail(entity_id, limit=limit, **_scoped(scope))

    def timeline(self, *, include_review: bool = True, start: date | None = None, end: date | None = None,
                 limit: int = 2000, scope: Visibility | None = None) -> dict[str, Any]:
        return self._inner.timeline(include_review=include_review, start=start, end=end, limit=limit, **_scoped(scope))

    def conflict_candidates(self, limit: int) -> list[Any]:
        return self._inner.conflict_candidates(limit)

    def entity_names(self) -> list[tuple[str, str]]:
        """批次 18a：缓存；知识数据不变时返回同一个对象（不做深拷贝，调用方只读），供 NameDirectory 判断是否需要重建词典。"""
        revision, now = self._revision(), self._clock()
        with self._lock:
            entry = self._entries.get(("entity_names",))
            if entry is not None and entry[0] == revision and now - entry[1] < self._max_age:
                return entry[2]
        value = self._inner.entity_names()
        with self._lock:
            self._store(("entity_names",), (revision, now, value))
        return value

    def review_context(self, kind: Any, target: str, value: str | None) -> Any:
        return self._inner.review_context(kind, target, value)
