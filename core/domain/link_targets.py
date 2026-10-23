"""引用目标的物化（批次 18b）：把每条引用对全部已发布文档标题的匹配结果存下来，读取时按可读范围过滤。规格见
docs/spec-33-link-targets.md。

resolve_title 每次读取都对每条引用遍历全部标题（并逐个规范化），代价为 引用数 × 文档数。这里改为：
- 物化：每条引用存 exact（规范化标题与书名完全相同的已发布文档）与 fuzzy（相互包含、双方都不短于 MIN_FUZZY_CHARS、
  但不完全相同的已发布文档），都不排除引用自身所在的版本；同时记下计算时的已发布文档集合 {version_id: 规范化标题}。
- 增量：plan_sync 比较记下的文档集合、引用集合与当前的，只计算变化部分；标题匹配用 TitleIndex（完全相同查字典，
  “标题含于书名”枚举书名的子串，“书名含于标题”用二元组倒排求候选再核对）。
- 读取：resolve_stored(targets, pool) 在 pool（可读的已发布文档）内去掉自身版本后，给出与 resolve_title(title, pool 中的文档,
  exclude=version_id) 完全相同的结果；build_links_from 给出与 build_links 完全相同的视图。
"""
from __future__ import annotations

from collections.abc import Container, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.domain.relations import (MIN_FUZZY_CHARS, MIN_TITLE_CHARS, ReferenceRecord, TitleMatch, TitleResolution,
                                   normalize_title)


@dataclass(frozen=True)
class RefTargets:
    """一条引用的物化匹配。key 为 normalize_title(书名)；exact、fuzzy 为版本号，升序、互不相交。"""
    ref_id: str
    version_id: str
    key: str
    exact: tuple[str, ...] = ()
    fuzzy: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"ref_id": self.ref_id, "version_id": self.version_id, "key": self.key, "exact": list(self.exact),
                "fuzzy": list(self.fuzzy)}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RefTargets:
        return cls(data["ref_id"], data["version_id"], data["key"], tuple(data["exact"]), tuple(data["fuzzy"]))


@dataclass(frozen=True)
class TargetState:
    """docs：计算时的已发布文档 {version_id: 规范化标题}；targets：{ref_id: RefTargets}；version：每次写入加一。"""
    docs: dict[str, str] = field(default_factory=dict)
    targets: dict[str, RefTargets] = field(default_factory=dict)
    version: int = 0


@dataclass(frozen=True)
class TargetPlan:
    """一次同步要写入的变化：docs_set 覆盖写入，docs_delete 删除（先删后写）；targets_set 覆盖写入，targets_delete 删除。"""
    docs_set: dict[str, str] = field(default_factory=dict)
    docs_delete: frozenset[str] = frozenset()
    targets_set: tuple[RefTargets, ...] = ()
    targets_delete: frozenset[str] = frozenset()

    @property
    def empty(self) -> bool:
        return not (self.docs_set or self.docs_delete or self.targets_set or self.targets_delete)


def apply_plan(state: TargetState, plan: TargetPlan) -> TargetState:
    """在内存中应用 plan（存储实现的参考语义）：plan 为空时原样返回；否则 version 加一。"""
    if plan.empty:
        return state
    docs = {v: k for v, k in state.docs.items() if v not in plan.docs_delete}
    docs.update(plan.docs_set)
    targets = {r: t for r, t in state.targets.items() if r not in plan.targets_delete}
    targets.update((t.ref_id, t) for t in plan.targets_set)
    return TargetState(docs, targets, state.version + 1)


class TitleIndex:
    """已发布文档标题的索引。docs 为 {version_id: 规范化标题}。"""

    def __init__(self, docs: Mapping[str, str]) -> None:
        self._exact: dict[str, set[str]] = {}
        self._grams: dict[str, set[str]] = {}
        for version, key in docs.items():
            self._exact.setdefault(key, set()).add(version)
        for key in self._exact:
            if len(key) >= MIN_FUZZY_CHARS:
                for gram in _grams(key):
                    self._grams.setdefault(gram, set()).add(key)

    def matches(self, key: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """(exact, fuzzy)：len(key) < MIN_TITLE_CHARS → 两者都空。exact：标题等于 key 的版本；
        fuzzy（len(key) ≥ MIN_FUZZY_CHARS 时）：标题不短于 MIN_FUZZY_CHARS、不等于 key、与 key 互相包含的版本。都按升序。"""
        if len(key) < MIN_TITLE_CHARS:
            return (), ()
        exact = tuple(sorted(self._exact.get(key, ())))
        if len(key) < MIN_FUZZY_CHARS:
            return exact, ()
        keys: set[str] = set()
        for start in range(len(key)):                       # 标题含于书名：枚举书名的子串
            for end in range(start + MIN_FUZZY_CHARS, len(key) + 1):
                piece = key[start:end]
                if piece != key and piece in self._exact:
                    keys.add(piece)
        postings = sorted((self._grams.get(gram, set()) for gram in _grams(key)), key=len)
        if postings and postings[0]:                          # 书名含于标题：二元组倒排求候选再核对
            candidates = set(postings[0]).intersection(*postings[1:])
            keys.update(value for value in candidates if value != key and key in value)
        fuzzy = sorted({version for value in keys for version in self._exact[value]})
        return exact, tuple(fuzzy)


def _grams(key: str) -> set[str]:
    return {key[i:i + 2] for i in range(len(key) - 1)}


def plan_sync(state: TargetState, published: Mapping[str, str],
              references: Iterable[tuple[str, str, str]]) -> TargetPlan:
    """state 为当前物化结果，published 为当前已发布文档 {version_id: 标题}，references 为全部引用 (ref_id, version_id, 书名)。
    返回使物化结果等于“对 published 全量重算”的最小变化：
    - 文档：标题规范化后与记下的不同、或已不在 published 中的记为移除；新出现或标题变化的记为新增。
    - 引用：不在 references 中的删除；新出现、书名或所在版本变化的用全部 published 重新计算；
      其余在有文档移除或新增时，去掉移除的版本、加上新增文档中的匹配，结果有变化才写入。"""
    current = {version: normalize_title(title) for version, title in published.items()}
    removed = {version for version, key in state.docs.items() if current.get(version) != key}
    added = {version: key for version, key in current.items() if state.docs.get(version) != key}
    refs = {ref_id: (version, normalize_title(title)) for ref_id, version, title in references}
    targets_delete = frozenset(ref_id for ref_id in state.targets if ref_id not in refs)
    full: TitleIndex | None = None
    delta = TitleIndex(added) if added else None
    full_cache: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    delta_cache: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    changed: list[RefTargets] = []
    for ref_id, (version, key) in sorted(refs.items()):
        old = state.targets.get(ref_id)
        if old is None or old.key != key or old.version_id != version:
            if full is None:
                full = TitleIndex(current)
            if key not in full_cache:
                full_cache[key] = full.matches(key)
            exact, fuzzy = full_cache[key]
            changed.append(RefTargets(ref_id, version, key, exact, fuzzy))
            continue
        if not removed and delta is None:
            continue
        exact = [v for v in old.exact if v not in removed]
        fuzzy = [v for v in old.fuzzy if v not in removed]
        if delta is not None:
            if key not in delta_cache:
                delta_cache[key] = delta.matches(key)
            exact += delta_cache[key][0]
            fuzzy += delta_cache[key][1]
        new = RefTargets(ref_id, version, key, tuple(sorted(exact)), tuple(sorted(fuzzy)))
        if new != old:
            changed.append(new)
    return TargetPlan(added, frozenset(removed - set(added)), tuple(changed), targets_delete)


def resolve_stored(targets: RefTargets, pool: Container[str]) -> TitleResolution:
    """与 resolve_title(书名, {v: 标题 for v in pool}, exclude=targets.version_id) 相同（pool 须是物化时已发布文档的子集）。"""
    exact = tuple(v for v in targets.exact if v in pool and v != targets.version_id)
    if exact:
        return TitleResolution(TitleMatch.MATCHED if len(exact) == 1 else TitleMatch.CANDIDATES, exact)
    fuzzy = tuple(v for v in targets.fuzzy if v in pool and v != targets.version_id)
    return TitleResolution(TitleMatch.CANDIDATES if fuzzy else TitleMatch.UNRESOLVED, fuzzy)


def build_links_from(records: Iterable[ReferenceRecord], documents: Mapping[str, str],
                     targets: Mapping[str, RefTargets]) -> dict[str, list[dict[str, Any]]]:
    """与 build_links(records, documents) 结果相同，但用物化的匹配。某条引用没有物化结果、或书名、版本与物化时不一致时，
    为它单独用 resolve_title 计算（保证正确，代价只落在这几条上）。"""
    from core.domain.relations import resolve_title

    result: dict[str, list[dict[str, Any]]] = {"links": [], "pending": [], "unresolved": []}
    for record in sorted(records, key=lambda r: r.ref_id):
        if record.version_id not in documents:
            continue
        row = {"id": record.ref_id, "kind": record.kind.value, "reverse": record.reverse,
               "version_id": record.version_id, "title": record.title, "chunk_id": record.chunk_id,
               "quote": record.quote, "page_start": record.page_start, "page_end": record.page_end,
               "section_path": record.section_path}
        stored = targets.get(record.ref_id)
        if stored is not None and stored.version_id == record.version_id and stored.key == normalize_title(record.title):
            match = resolve_stored(stored, documents)
        else:
            match = resolve_title(record.title, documents, exclude=record.version_id)
        if match.kind is TitleMatch.MATCHED:
            source, target = record.version_id, match.version_ids[0]
            if record.reverse:
                source, target = target, source
            row.update(source=source, target=target, source_title=documents[source], target_title=documents[target])
            result["links"].append(row)
        elif match.kind is TitleMatch.CANDIDATES:
            row["candidates"] = [{"version_id": version, "title": documents[version]} for version in match.version_ids]
            result["pending"].append(row)
        else:
            result["unresolved"].append(row)
    return result
