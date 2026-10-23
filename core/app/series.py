"""批次 23c：文档系列的用例层。规格见 docs/spec-42-document-series.md。

系列由文档记录与审核决定计算得出（core.domain.series.build_series），不单独存储。结果按登记处的知识事件序号缓存：
文档发布、撤下、删除与审核决定变化都会追加知识事件，序号变了就重新计算。
所有对外结果都按可读范围裁剪：看不到的文档不出现，也不计数；一个可读成员都没有的系列视为不存在。
"""
from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from core.domain.permissions import Visibility
from core.domain.review import Decision, DecisionKind
from core.domain.series import Series, SeriesView, build_series, document_target, make_series_decision


class _Registry(Protocol):
    def documents(self) -> list[dict[str, Any]]: ...

    def last_event_seq(self) -> int: ...


class _Decisions(Protocol):
    def decisions(self) -> list[Decision]: ...


def _period(record: Mapping[str, Any]) -> dict[str, Any] | None:
    return record.get("period") or None


class SeriesService:
    def __init__(self, registry: _Registry, decisions: _Decisions) -> None:
        self._registry, self._decisions = registry, decisions
        self._lock = threading.Lock()
        self._cached: tuple[int, list[dict[str, Any]], SeriesView] | None = None

    # —— 计算与缓存 ——

    def _state(self) -> tuple[list[dict[str, Any]], SeriesView]:
        revision = self._registry.last_event_seq()            # 先取序号再读数据：读的过程中有写入，下次一定重算
        with self._lock:
            if self._cached is not None and self._cached[0] == revision:
                return self._cached[1], self._cached[2]
        records = self._registry.documents()
        view = build_series(records, self._decisions.decisions())
        with self._lock:
            self._cached = (revision, records, view)
        return records, view

    def view(self) -> SeriesView:
        return self._state()[1]

    @staticmethod
    def _readable(series: Series, by_version: Mapping[str, Mapping[str, Any]],
                  scope: Visibility | None) -> list[str]:
        if scope is None:
            return list(series.members)
        return [v for v in series.members if scope.contains(v, by_version[v].get("folder", ""))]

    # —— 查询 ——

    def list(self, scope: Visibility | None = None, *, query: str = "") -> list[dict[str, Any]]:
        """有可读成员的系列：Series.to_json(可读成员) 加 "first"、"last"（可读成员中第一期、最后一期的
        {"version_id","title","period"}）。query 非空时只保留名称包含它（casefold）的。按 (name, id) 排序。"""
        records, view = self._state()
        by_version = {row["version_id"]: row for row in records}
        needle = query.strip().casefold()
        rows = []
        for series in view.series:
            members = self._readable(series, by_version, scope)
            if not members or (needle and needle not in series.name.casefold()):
                continue
            rows.append({**series.to_json(members), "first": self._brief(by_version[members[0]]),
                         "last": self._brief(by_version[members[-1]])})
        return rows

    @staticmethod
    def _brief(record: Mapping[str, Any]) -> dict[str, Any]:
        return {"version_id": record["version_id"], "title": record.get("title"), "period": _period(record)}

    def detail(self, series_id: str, scope: Visibility | None = None) -> dict[str, Any] | None:
        """{"series": to_json(可读成员), "members": [{"version_id","title","filename","folder","period",
        "imported_at","position"(在可读成员中从 1 起)}…按期次], "redirected_from": 原 ID（经合并重定向时）或 None}；
        系列不存在或没有可读成员 → None。"""
        records, view = self._state()
        series = view.get(series_id)
        if series is None:
            return None
        by_version = {row["version_id"]: row for row in records}
        members = self._readable(series, by_version, scope)
        if not members:
            return None
        rows = [{"version_id": v, "title": by_version[v].get("title"), "filename": by_version[v].get("filename"),
                 "folder": by_version[v].get("folder", ""), "period": _period(by_version[v]),
                 "imported_at": by_version[v].get("imported_at"), "position": index}
                for index, v in enumerate(members, start=1)]
        return {"series": series.to_json(members), "members": rows,
                "redirected_from": series_id if series.id != series_id else None}

    def of_document(self, version_id: str, scope: Visibility | None = None) -> dict[str, Any] | None:
        """文档所属系列（文档本身须是成员）：{"id","name","position","total","prev","next"}，position、total 与前后期
        都只按可读成员计算；prev / next 为 {"version_id","title","period"} 或 None。不是成员 → None。"""
        records, view = self._state()
        series_id = view.of_version.get(version_id)
        if series_id is None:
            return None
        series = view.get(series_id)
        by_version = {row["version_id"]: row for row in records}
        members = self._readable(series, by_version, scope)
        if version_id not in members:
            return None
        index = members.index(version_id)
        return {"id": series.id, "name": series.name, "position": index + 1, "total": len(members),
                "prev": self._brief(by_version[members[index - 1]]) if index > 0 else None,
                "next": self._brief(by_version[members[index + 1]]) if index + 1 < len(members) else None}

    def labels(self, version_ids: Iterable[str]) -> dict[str, dict[str, str]]:
        """{version_id: {"id","name"}}：给出的版本中属于某系列的（文档库每行的系列标签）。"""
        view = self.view()
        names = {row.id: row.name for row in view.series}
        return {v: {"id": view.of_version[v], "name": names[view.of_version[v]]}
                for v in version_ids if v in view.of_version}

    def scope_for(self, series_id: str, scope: Visibility | None = None) -> Visibility | None:
        """把查询范围限定为该系列的可读成员（时间轴、图谱按系列筛选）：Visibility(folders=∅, allow=可读成员)。
        系列不存在或没有可读成员 → None。"""
        records, view = self._state()
        series = view.get(series_id)
        if series is None:
            return None
        members = self._readable(series, {row["version_id"]: row for row in records}, scope)
        return Visibility(frozenset(), frozenset(members)) if members else None

    # —— 审核决定 ——

    def decide(self, kind: DecisionKind, target: str, value: str | None, *, number: int, created_at: str,
               actor: str | None = None) -> Decision:
        """make_series_decision(...)，view 与 records 现算（不用缓存），保证校验基于最新状态。"""
        records = self._registry.documents()
        view = build_series(records, self._decisions.decisions())
        extra = {} if actor is None else {"actor": actor}
        return make_series_decision(kind, target, value, view=view, records=records, number=number,
                                    created_at=created_at, **extra)

    def decision_visible(self, kind: DecisionKind, target: str, value: str | None,
                         scope: Visibility | None) -> bool:
        """账号能否看到并修改该系列决定的对象（看不到 → 接口按不存在处理，返回 404）。scope 为 None（开放模式）恒为真。
        - SERIES_CREATE：恒为真。
        - SERIES_RENAME：target 系列的全部成员都可读（系列不存在时为真，由 decide 报 KeyError）。
        - SERIES_MERGE：target 与 value 两个系列的全部成员都可读（同上）。
        - DOCUMENT_SERIES：target 文档（最后保存的已发布版本）可读；value 非空时，该系列有可读成员或没有任何成员
          （刚新建的人工系列），且存在。文档不存在时为真（由 decide 报 KeyError）。"""
        if scope is None or kind is DecisionKind.SERIES_CREATE:
            return True
        records, view = self._state()
        by_version = {row["version_id"]: row for row in records}

        def whole(series_id: str | None) -> bool:
            series = view.get(series_id or "")
            return series is None or len(self._readable(series, by_version, scope)) == len(series.members)

        if kind is DecisionKind.SERIES_RENAME:
            return whole(target)
        if kind is DecisionKind.SERIES_MERGE:
            return whole(target) and whole(value)
        if kind is DecisionKind.DOCUMENT_SERIES:
            documents = [row for row in records if row.get("status") == "published" and document_target(row) == target]
            if documents and not scope.contains(documents[-1]["version_id"], documents[-1].get("folder", "")):
                return False
            if value:
                series = view.get(value)
                if series is not None and not self._readable(series, by_version, scope):
                    return False
            return True
        return True

