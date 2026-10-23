"""知识数据（实体、事件、提及、引用、冲突判断、审核决定）的存储端口与查询端口。规格见 docs/spec-25-knowledge-db.md。

- 存储端口 KnowledgeRepo / LinkRepo / DecisionRepo：原始数据的读写，方法与批次 10–12 的 JSON 存储一致。
  实现：apps/web 中的 KnowledgeStore / LinkStore / DecisionStore（JSON）与 core/adapters/pg_knowledge.py（PostgreSQL）。
- 查询端口 KnowledgeQueries：界面与后台作业需要的“叠加审核决定后的有效视图”的各种取数。
  语义参考实现是本文件的 FullScanQueries：读出全部原始数据，用领域层纯函数计算。
  PostgreSQL 实现只取需要的那部分数据，但对任何数据集、任何参数，结果必须与 FullScanQueries 完全相同
  （tests/contracts/knowledge_queries.py 用随机数据集做差分验证）。
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import date
from typing import Any, Protocol, runtime_checkable

from core.app.extract import DocumentKnowledge
from core.app.link import DocumentReferences
from core.app.registry import DocumentRegistry
from core.domain.entity_graph import build_entity_graph
from core.domain.entity_relations import RelationRecord, entity_relation_rows
from core.domain.events import Verdict
from core.domain.knowledge_scope import scope_records
from core.domain.link_targets import TargetPlan, TargetState
from core.domain.permissions import Visibility
from core.domain.knowledge import (DETAIL_LIMIT, EntityRecord, EventRecord, MentionRecord, build_timeline,
                                   entity_detail, timeline_window)
from core.domain.relations import ConflictRecord, ReferenceRecord, build_conflicts, conflict_candidates
from core.domain.review import (Decision, DecisionKind, ReviewContext, effective_entities, effective_events,
                                effective_mentions, effective_relations, merge_map)

REVIEW_LIMIT = 200
CANDIDATE_COUNT_CAP = 10_000


@runtime_checkable
class KnowledgeRepo(Protocol):
    def entities(self) -> list[EntityRecord]:
        """全部原始实体，按创建顺序。"""

    def extracted(self) -> set[str]:
        """已有抽取结果的 version_id 集合。"""

    def save(self, knowledge: DocumentKnowledge, *, extracted_at: str) -> None:
        """保存一个版本的抽取结果（已存在则整体替换）；新实体追加。版本号格式无效、新实体 ID 与已有重复 → ValueError，不写入。"""

    def save_with(self, build: Callable[[list[EntityRecord]], DocumentKnowledge], *,
                  extracted_at: str) -> DocumentKnowledge:
        """批次 16b-Q5：在跨线程、跨进程互斥的临界区内，用当时的全部实体调用 build(实体列表)，
        再按 save 的规则保存其结果并返回。build 抛出的异常原样抛出，不写入任何东西。
        用于新实体 ID 的分配：build 读到的实体就是保存时的实体，并发抽取不会分到相同的 ID。build 应只做纯计算。"""

    def events(self) -> list[EventRecord]:
        """全部原始事件，按 event_id 升序。"""

    def mentions(self) -> list[MentionRecord]:
        """全部原始提及，按 (version_id, 原有顺序)。"""

    def relations(self) -> list[RelationRecord]:
        """批次 23b：全部原始实体关系，按 relation_id 升序。"""

    def document(self, version_id: str) -> dict[str, Any] | None:
        """{"version_id","extracted_at","report","events","mentions","relations"（批次 23b）}（to_json 形式）；没有为 None。"""

    def purge_document(self, version_id: str, mapping: Mapping[str, str]) -> dict[str, Any]:
        """批次 20b：删除该版本的抽取结果（事件与提及），再把无人提及的实体（orphan_entities，mapping 为当前合并去向）
        抹去名称（scrub_entity；已抹去的不再改）。返回 {"events": 删除的事件数, "mentions": 删除的提及数,
        "relations": 删除的关系数（批次 23b）, "entities_scrubbed": [本次抹去名称的实体 ID，升序]}。没有抽取结果时前两项为 0，仍检查无人提及的实体。可以重做。"""


@runtime_checkable
class LinkRepo(Protocol):
    def extracted(self) -> set[str]: ...

    def save_references(self, result: DocumentReferences, *, extracted_at: str) -> None: ...

    def references(self) -> list[ReferenceRecord]:
        """全部引用记录，按 ref_id 升序。"""

    def document(self, version_id: str) -> dict[str, Any] | None: ...

    def conflicts(self) -> list[ConflictRecord]:
        """全部已判断的事件对，按 pair_id 升序。"""

    def judged(self) -> set[str]: ...

    def add_conflict(self, record: ConflictRecord) -> None: ...

    def purge_document(self, version_id: str) -> dict[str, int]:
        """批次 20b：删除该版本的引用提取结果，以及一方属于该版本的冲突判断。
        返回 {"references": 删除的引用数, "conflicts": 删除的冲突判断数}。可以重做（第二次都为 0）。"""

    # 批次 18b：引用目标的物化（core/domain/link_targets.py，docs/spec-33-link-targets.md）。
    def link_targets(self) -> TargetState:
        """当前物化结果（调用方可以随意使用，不影响存储）。"""

    def link_targets_version(self) -> int:
        """物化结果的版本号：每次非空写入加一（读取它应当很便宜，用于判断进程内的副本是否过期）。"""

    def sync_link_targets(self, plan: Callable[[TargetState], TargetPlan]) -> TargetState:
        """在锁（或事务）内读取最新的物化结果 s，调用 plan(s) 得到变化并写入（apply_plan 的语义），返回写入后的结果。
        plan 返回空变化时不写入、版本号不变。plan 抛出异常时不写入任何内容，异常原样抛出。"""


@runtime_checkable
class DecisionRepo(Protocol):
    def decisions(self) -> list[Decision]:
        """全部决定，按编号升序。"""

    def create(self, factory: Callable[[int], Decision]) -> Decision:
        """在锁（或事务）内调用 factory(序号) 生成并保存决定；去掉 (group, target) 相同的旧决定；序号只增不减。"""

    def remove(self, decision_id: str) -> Decision | None: ...

    def rewrite(self, decisions: Iterable[Decision]) -> None:
        """批次 20b：按 decision_id 原位替换已有决定（种类、对象必须与原来相同），编号与顺序不变。
        任何一个 ID 不存在或种类、对象不同 → KeyError，全部不写。"""


@runtime_checkable
class KnowledgeQueries(Protocol):
    """叠加审核决定后的有效视图取数。语义以 FullScanQueries 为准。
    批次 16b：界面用的方法多了 scope（Visibility，见 core/domain/permissions.py）；None 表示不限（后台作业、开放模式）。
    给出 scope 时，先按 scope 求出可读的文档版本集合 V（全部登记文档中 scope.contains(version_id, folder) 为真的），
    再按 core/domain/knowledge_scope.scope_records 裁剪有效视图，并把文档标题、已发布文档都限制在 V 内，然后照常计算。"""

    def merge_map(self) -> dict[str, str]: ...

    def entity_detail(self, entity_id: str, *, limit: int = DETAIL_LIMIT,
                      scope: Visibility | None = None) -> dict[str, Any] | None: ...

    def entity_graph(self, *, include_review: bool = True, focus: str | None = None, depth: int = 1,
                     limit: int = 300, scope: Visibility | None = None) -> dict[str, Any]: ...

    def timeline(self, *, include_review: bool = True, start: date | None = None, end: date | None = None,
                 limit: int = 2000, scope: Visibility | None = None) -> dict[str, Any]: ...

    def knowledge_counts(self, *, scope: Visibility | None = None) -> dict[str, Any]: ...

    def review_entities(self, limit: int = REVIEW_LIMIT, *, scope: Visibility | None = None) -> list[dict[str, Any]]: ...

    def review_events(self, limit: int = REVIEW_LIMIT, *, scope: Visibility | None = None) -> list[dict[str, Any]]: ...

    def conflict_candidates(self, limit: int) -> list[tuple[EventRecord, EventRecord]]: ...

    def conflict_candidate_count(self, cap: int = CANDIDATE_COUNT_CAP) -> int: ...

    def conflict_rows(self, *, scope: Visibility | None = None) -> list[dict[str, Any]]: ...

    def review_context(self, kind: DecisionKind, target: str, value: str | None) -> ReviewContext: ...

    def entity_names(self) -> list[tuple[str, str]]:
        """批次 18a：实体词典的来源，[(名称, 最终实体 ID)]。对每个实体记录（含已被合并的），其 canonical 与 aliases 各一项，
        实体 ID 经 merge_map 重定向到最终实体；去重后按 (名称, 实体 ID) 升序。不受 scope 限制（名称的存在不算越权，规格 29）。"""


class FullScanQueries:
    """KnowledgeQueries 的语义参考实现：每次调用读出全部原始数据，用纯函数计算。JSON 存储下直接使用。"""

    def __init__(self, registry: DocumentRegistry, knowledge: KnowledgeRepo, links: LinkRepo,
                 decisions: DecisionRepo) -> None:
        self._registry, self._knowledge, self._links, self._decisions = registry, knowledge, links, decisions

    # —— 公共部分 ——

    def _visible(self, scope: Visibility | None) -> set[str]:
        """知识视图可用的文档版本（批次 20a）：当前为已发布，且给出 scope 时在可读范围内。
        已撤下、已替代、未发布文档的抽取结果不出现在任何知识视图中（实体详情、时间轴、图谱、计数、审核列表、冲突）。"""
        return {row["version_id"] for row in self._registry.documents() if row["status"] == "published"
                and (scope is None or scope.contains(row["version_id"], row.get("folder", "")))}

    def _titles(self, scope: Visibility | None = None) -> dict[str, str]:
        visible = self._visible(scope)
        return {row["version_id"]: row["title"] for row in self._registry.documents()
                if visible is None or row["version_id"] in visible}

    def _published(self, scope: Visibility | None = None) -> dict[str, str]:
        visible = self._visible(scope)
        return {row["version_id"]: row["title"] for row in self._registry.documents() if row["status"] == "published"
                and (visible is None or row["version_id"] in visible)}

    def _view(self, scope: Visibility | None = None
              ) -> tuple[dict[str, str], list[EntityRecord], list[MentionRecord], list[EventRecord]]:
        """(merge_map, 有效实体, 有效提及, 有效事件)；给出 scope 时三者按可读文档裁剪（merge_map 不裁剪）。"""
        decisions = self._decisions.decisions()
        raw = self._knowledge.entities()
        mapping = merge_map(raw, decisions)
        entities = effective_entities(raw, decisions)
        mentions = effective_mentions(self._knowledge.mentions(), mapping)
        events = effective_events(self._knowledge.events(), decisions, mapping)
        entities, mentions, events = scope_records(entities, mentions, events, self._visible(scope))
        return mapping, entities, mentions, events

    def _relations(self, scope: Visibility | None, mapping: Mapping[str, str]) -> list[RelationRecord]:
        """批次 23b：有效关系（effective_relations），只保留 version_id 在可读集合中的。"""
        visible = self._visible(scope)
        return [relation for relation in effective_relations(self._knowledge.relations(), self._decisions.decisions(),
                                                             mapping) if relation.version_id in visible]

    def merge_map(self) -> dict[str, str]:
        """merge_map(原始实体, 全部决定)。"""
        return merge_map(self._knowledge.entities(), self._decisions.decisions())

    # —— 界面 ——

    def entity_detail(self, entity_id: str, *, limit: int = DETAIL_LIMIT,
                      scope: Visibility | None = None) -> dict[str, Any] | None:
        """entity_id 先经 merge_map 重定向；重定向后不在有效实体中（给出 scope 时为裁剪后的实体）→ None。
        否则为 entity_detail(...)，重定向过时带 redirected_from（原 ID）。
        批次 23b：另加 "relations"、"relations_truncated" = entity_relation_rows(最终 ID, 有效可读关系, 有效（裁剪后）实体,
        文档标题, limit=limit)。"""
        mapping, entities, mentions, events = self._view(scope)
        final = mapping.get(entity_id, entity_id)
        by_id = {entity.id: entity for entity in entities}
        if final not in by_id:
            return None
        counts = Counter(mention.version_id for mention in mentions if mention.entity_id == final)
        titles = self._titles(scope)
        data = entity_detail(by_id[final], by_id, counts, events, titles,
                             redirected_from=entity_id if final != entity_id else None, limit=limit)
        data["relations"], data["relations_truncated"] = entity_relation_rows(
            final, self._relations(scope, mapping), by_id, titles, limit=limit)
        return data

    def entity_graph(self, *, include_review: bool = True, focus: str | None = None, depth: int = 1,
                     limit: int = 300, scope: Visibility | None = None) -> dict[str, Any]:
        """build_entity_graph(已发布文档, 有效实体, 有效提及, 有效事件, ...)；参数错误或 focus 不存在 → ValueError。
        批次 23b-r2：relations=有效可读关系，关系边与事件边一起参与构图（规格 41 第九节）。"""
        mapping, entities, mentions, events = self._view(scope)
        return build_entity_graph(self._published(scope), entities, mentions, events, include_review=include_review,
                                  focus=focus, depth=depth, limit=limit, relations=self._relations(scope, mapping))

    def timeline(self, *, include_review: bool = True, start: date | None = None, end: date | None = None,
                 limit: int = 2000, scope: Visibility | None = None) -> dict[str, Any]:
        """timeline_window(有效事件, 文档标题, ...)。批次 20a 起只含已发布文档的事件（原先不限于已发布，见规格 36 第四节）。"""
        _, _, _, events = self._view(scope)
        return timeline_window(events, self._titles(scope), include_review=include_review, start=start, end=end, limit=limit)

    def knowledge_counts(self, *, scope: Visibility | None = None) -> dict[str, Any]:
        """{"extracted": 已抽取版本数, "entities": 有效实体数, "events": {pass, review, reject, duplicates}}。
        pass/review 不含 duplicate_of 非空的事件；reject 含全部被拒绝的事件；duplicates 为 duplicate_of 非空的事件数。
        给出 scope 时 extracted 只数可读的版本。"""
        _, entities, _, events = self._view(scope)
        visible = self._visible(scope)
        counts = {verdict.value: sum(event.verdict is verdict and (verdict is Verdict.REJECT or not event.duplicate_of)
                                     for event in events) for verdict in Verdict}
        counts["duplicates"] = sum(bool(event.duplicate_of) for event in events)
        extracted = self._knowledge.extracted()
        return {"extracted": len(extracted if visible is None else extracted & visible), "entities": len(entities),
                "events": counts}

    def review_entities(self, limit: int = REVIEW_LIMIT, *, scope: Visibility | None = None) -> list[dict[str, Any]]:
        """有效 review_candidates 非空的有效实体，按 ID 升序前 limit 个：
        {"id","type","subtype"（批次 23a）,"canonical","candidates": [{"id","canonical"}…（候选在有效实体中的，保持顺序）]}。
        给出 scope 时实体已裁剪，review_candidates 只剩可见的候选，候选全被裁掉的实体不再列出。"""
        _, entities, _, _ = self._view(scope)
        by_id = {entity.id: entity for entity in entities}
        rows = [{"id": entity.id, "type": entity.type.value, "subtype": entity.subtype, "canonical": entity.canonical,
                 "candidates": [{"id": id_, "canonical": by_id[id_].canonical}
                                for id_ in entity.review_candidates if id_ in by_id]}
                for entity in sorted(entities, key=lambda entity: entity.id) if entity.review_candidates]
        return rows[:limit]

    def review_events(self, limit: int = REVIEW_LIMIT, *, scope: Visibility | None = None) -> list[dict[str, Any]]:
        """有效 verdict 为 REVIEW 的事件（全部文档）经 build_timeline 后的行，合并 items 与 undated 按 id 升序，前 limit 条。"""
        _, _, _, events = self._view(scope)
        timeline = build_timeline((event for event in events if event.verdict is Verdict.REVIEW), self._titles(scope))
        return sorted(timeline["items"] + timeline["undated"], key=lambda row: row["id"])[:limit]

    # —— 冲突判断 ——

    def _published_events(self) -> list[EventRecord]:
        published = self._published()
        return [event for event in self._view()[3] if event.version_id in published]

    def conflict_candidates(self, limit: int) -> list[tuple[EventRecord, EventRecord]]:
        """conflict_candidates(已发布文档的有效事件, judged=已判断, limit=limit)。"""
        return conflict_candidates(self._published_events(), judged=self._links.judged(), limit=limit)

    def conflict_candidate_count(self, cap: int = CANDIDATE_COUNT_CAP) -> int:
        """尚未判断的候选事件对数量，最多数到 cap（界面显示为“cap+”）；cap < 1 → ValueError。"""
        if cap < 1:
            raise ValueError("计数上限必须为正整数")
        return len(conflict_candidates(self._published_events(), judged=self._links.judged(), limit=cap))

    def conflict_rows(self, *, scope: Visibility | None = None) -> list[dict[str, Any]]:
        """build_conflicts(全部判断结果, 有效且未被拒绝的事件, 全部标题, 已发布版本)（未叠加冲突决定）。
        给出 scope 时只剩两边文档都可读的行。"""
        events = {event.event_id: event for event in self._view(scope)[3] if event.verdict is not Verdict.REJECT}
        return build_conflicts(self._links.conflicts(), events, self._titles(scope), set(self._published(scope)))

    # —— 审核决定 ——

    def entity_names(self) -> list[tuple[str, str]]:
        mapping = merge_map(self._knowledge.entities(), self._decisions.decisions())
        return sorted({(name, mapping.get(entity.id, entity.id)) for entity in self._knowledge.entities()
                       for name in (entity.canonical, *entity.aliases)})

    def review_context(self, kind: DecisionKind, target: str, value: str | None) -> ReviewContext:
        """创建决定用的上下文：原始实体、原始事件、全部引用、已发布文档、conflict_rows()、未收录问题、全部决定。
        其他实现可以只给出与 (kind, target, value) 相关的子集，但 make_decision 的结果（含异常类型与消息）必须相同。"""
        return ReviewContext(entities=self._knowledge.entities(), events=self._knowledge.events(),
                             references=self._links.references(), documents=self._published(),
                             conflicts=self.conflict_rows(), questions=self._registry.unanswered(),
                             decisions=self._decisions.decisions(), relations=self._knowledge.relations())


