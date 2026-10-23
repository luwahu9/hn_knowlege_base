"""审核决定：人工对抽取结果的确认、驳回与合并（纯逻辑）。规格：docs/spec-20-review.md

抽取结果（实体、提及、事件、引用、冲突）保持只读，人工决定单独保存为 Decision 列表，读取时叠加，得到“有效视图”。
重新抽取会整体替换某文档的抽取结果；决定按稳定的键挂接，键或快照对不上的决定读取时被静默忽略。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from core.domain.entity_relations import RelationRecord, relation_label
from core.domain.events import Verdict
from core.domain.knowledge import EntityRecord, EventRecord, MentionRecord, event_label
from core.domain.relations import ReferenceRecord, normalize_title
from core.domain.taxonomy import SUBTYPES

DECISION_ID = re.compile(r"d[0-9]{6,}")
LOCAL_ACTOR = "本机"


class DecisionKind(str, Enum):
    ENTITY_MERGE = "entity_merge"            # 把 target 实体合并到 value 实体
    ENTITY_DISTINCT = "entity_distinct"      # target 实体与其候选都不是同一实体
    EVENT_PASS = "event_pass"                # 通过待审事件
    EVENT_REJECT = "event_reject"            # 驳回事件（待审或已通过）
    REFERENCE_SET = "reference_set"          # 引用指向 value 文档；value 为空串表示“库中没有”
    CONFLICT_CONFIRM = "conflict_confirm"    # 确认文档冲突
    CONFLICT_DISMISS = "conflict_dismiss"    # 判定不冲突
    QUESTION_DONE = "question_done"          # 未收录问题已处理
    RELATION_REJECT = "relation_reject"      # 批次 23b：否决实体关系
    ENTITY_SUBTYPE = "entity_subtype"        # 批次 23b：修改实体子类；value 为子类，"" 表示未分类
    SERIES_CREATE = "series_create"          # 批次 23c：新建文档系列；target 为 manual_series_id(编号)，value 为名称
    SERIES_RENAME = "series_rename"          # 批次 23c：系列改名；value 为新名称
    SERIES_MERGE = "series_merge"            # 批次 23c：把 target 系列并入 value 系列
    DOCUMENT_SERIES = "document_series"      # 批次 23c：target 文档（document_target）归入 value 系列；"" 表示不属于任何系列


GROUPS: dict[DecisionKind, str] = {
    DecisionKind.ENTITY_MERGE: "entity", DecisionKind.ENTITY_DISTINCT: "entity",
    DecisionKind.EVENT_PASS: "event", DecisionKind.EVENT_REJECT: "event",
    DecisionKind.REFERENCE_SET: "reference",
    DecisionKind.CONFLICT_CONFIRM: "conflict", DecisionKind.CONFLICT_DISMISS: "conflict",
    DecisionKind.QUESTION_DONE: "question",
    DecisionKind.RELATION_REJECT: "relation", DecisionKind.ENTITY_SUBTYPE: "subtype",
    DecisionKind.SERIES_CREATE: "series_create", DecisionKind.SERIES_RENAME: "series_name",
    DecisionKind.SERIES_MERGE: "series_merge", DecisionKind.DOCUMENT_SERIES: "doc_series",
}
SERIES_KINDS = frozenset({DecisionKind.SERIES_CREATE, DecisionKind.SERIES_RENAME, DecisionKind.SERIES_MERGE,
                          DecisionKind.DOCUMENT_SERIES})   # 批次 23c：由 core.domain.series.make_series_decision 生成


class ReviewError(ValueError):
    """决定不合法（目标存在，但当前状态不允许该操作）。目标不存在用 KeyError。"""


@dataclass(frozen=True)
class Decision:
    decision_id: str                 # decision_id(n)
    kind: DecisionKind
    target: str                      # 见各 kind 的说明（make_decision）
    value: str | None                # ENTITY_MERGE：合并到的实体 ID；REFERENCE_SET：版本号或 ""；ENTITY_SUBTYPE：子类或 ""；其余 None
    snapshot: tuple[str, ...]        # 事件、关系：(引文,)；冲突：(甲引文, 乙引文)；其余 ()
    label: str                       # 给人看的说明，创建时生成
    actor: str
    created_at: str                  # ISO 时间字符串，由调用方传入

    @property
    def group(self) -> str:
        return GROUPS[self.kind]

    def to_json(self) -> dict[str, Any]:
        """扁平字典，键名同属性名（不含 group）；kind 写枚举值，snapshot 写 list。"""
        return {**vars(self), "kind": self.kind.value, "snapshot": list(self.snapshot)}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Decision:
        """to_json 的逆运算；缺少键 → KeyError，kind 无效或 decision_id 不符合 DECISION_ID → ValueError。"""
        values = {key: data[key] for key in cls.__dataclass_fields__}
        decision_number(values["decision_id"])
        values["kind"] = DecisionKind(values["kind"])
        values["snapshot"] = tuple(values["snapshot"])
        return cls(**values)


def decision_id(number: int) -> str:
    """"d" + 至少 6 位十进制序号（d000001）。number < 1 → ValueError。"""
    if number < 1:
        raise ValueError("决定序号必须为正整数")
    return f"d{number:06d}"


def decision_number(value: str) -> int:
    """decision_id 的逆运算。不符合 DECISION_ID 全匹配 → ValueError。"""
    if DECISION_ID.fullmatch(value) is None:
        raise ValueError("决定 ID 格式无效")
    return int(value[1:])


def reference_key(version_id: str, kind: str, reverse: bool, title: str) -> str:
    """引用决定的稳定键：f"{version_id}|{kind}|{1 或 0}|{normalize_title(title)}"。kind 为关系枚举值字符串。"""
    return f"{version_id}|{kind}|{1 if reverse else 0}|{normalize_title(title)}"


def question_key(time: str, question: str) -> str:
    """未收录问题的键：f"{time}|{question}"。"""
    return f"{time}|{question}"


def latest(decisions: Iterable[Decision]) -> dict[tuple[str, str], Decision]:
    """每个 (group, target) 只保留决定编号最大的一条。返回 {(group, target): Decision}。"""
    result: dict[tuple[str, str], Decision] = {}
    for decision in decisions:
        key = (decision.group, decision.target)
        if key not in result or decision_number(decision.decision_id) > decision_number(result[key].decision_id):
            result[key] = decision
    return result


# ---------------------------------------------------------------- 实体
def merge_map(entities: Iterable[EntityRecord], decisions: Iterable[Decision]) -> dict[str, str]:
    """被合并实体 → 最终实体 的映射（只含被合并的实体）。

    取 latest() 中 kind 为 ENTITY_MERGE 的决定，按决定编号升序逐条考虑；一条决定有效需同时满足：
    target 与 value 都在 entities 中、两者不同、类型相同、加入后不形成环（value 沿已采纳的合并链最终不回到 target）。
    无效的决定忽略。最终实体 = 沿合并链走到底（不再被合并）的实体。
    """
    records = {entity.id: entity for entity in entities}
    parents: dict[str, str] = {}
    for decision in sorted(latest(decisions).values(), key=lambda row: decision_number(row.decision_id)):
        source, target = decision.target, decision.value
        if (decision.kind is not DecisionKind.ENTITY_MERGE or source not in records or target not in records
                or source == target or records[source].type is not records[target].type):
            continue
        final = target
        while final in parents:
            final = parents[final]
        if final != source:
            parents[source] = target
    result: dict[str, str] = {}
    for source in parents:
        target = parents[source]
        while target in parents:
            target = parents[target]
        result[source] = target
    return result


def effective_entities(entities: Sequence[EntityRecord], decisions: Iterable[Decision]) -> list[EntityRecord]:
    """有效实体（保持原顺序，去掉被合并的实体）。

    mapping = merge_map(entities, decisions)。对每个保留的实体 E：
    - aliases：E 原有别名，之后依次追加合并到 E 的每个实体（按实体 ID 升序）的 canonical 及其别名；
      去掉与 E.canonical 完全相同的、去重（保留首次出现的顺序）。
    - review_candidates：latest() 中有针对 E 的 ENTITY_DISTINCT 决定 → ()；否则把每个候选经 mapping 映射到最终实体，
      去掉等于 E.id 的、不在 entities 中的，去重（保留首次出现的顺序）。
    - subtype（批次 23b）：latest() 中 ("subtype", E.id) 的 ENTITY_SUBTYPE 决定 d：d.value 为 "" → None；
      d.value 在 SUBTYPES[E.type] 中 → d.value；其他（类型已不同等）→ 忽略。针对被合并实体的决定不生效。
    """
    decisions = tuple(decisions)
    mapping = merge_map(entities, decisions)
    newest = latest(decisions)
    ids = {entity.id for entity in entities}
    result: list[EntityRecord] = []
    for entity in entities:
        if entity.id in mapping:
            continue
        names = list(entity.aliases)
        for merged in sorted(entities, key=lambda row: row.id):
            if mapping.get(merged.id) == entity.id:
                names.extend((merged.canonical, *merged.aliases))
        aliases = tuple(dict.fromkeys(name for name in names if name != entity.canonical))
        decision = newest.get(("entity", entity.id))
        candidates = () if decision and decision.kind is DecisionKind.ENTITY_DISTINCT else tuple(dict.fromkeys(
            mapped for candidate in entity.review_candidates
            if (mapped := mapping.get(candidate, candidate)) in ids and mapped != entity.id))
        subtype = entity.subtype
        chosen = newest.get(("subtype", entity.id))
        if chosen is not None and chosen.kind is DecisionKind.ENTITY_SUBTYPE:
            if chosen.value == "":
                subtype = None
            elif chosen.value in SUBTYPES[entity.type]:
                subtype = chosen.value
        result.append(replace(entity, aliases=aliases, review_candidates=candidates, subtype=subtype))
    return result


def effective_relations(relations: Iterable[RelationRecord], decisions: Iterable[Decision],
                        mapping: Mapping[str, str]) -> list[RelationRecord]:
    """批次 23b：有效关系（保持顺序）。两端经 mapping 映射；latest() 中 ("relation", relation_id) 的 RELATION_REJECT 决定
    且 snapshot == (quote,) → 去掉；映射后两端相同（合并造成）→ 去掉。"""
    newest = latest(decisions)
    result: list[RelationRecord] = []
    for relation in relations:
        decision = newest.get(("relation", relation.relation_id))
        if decision and decision.kind is DecisionKind.RELATION_REJECT and decision.snapshot == (relation.quote,):
            continue
        source = mapping.get(relation.source_entity, relation.source_entity)
        target = mapping.get(relation.target_entity, relation.target_entity)
        if source == target:
            continue
        result.append(replace(relation, source_entity=source, target_entity=target)
                      if (source, target) != (relation.source_entity, relation.target_entity) else relation)
    return result


def effective_mentions(mentions: Iterable[MentionRecord], mapping: Mapping[str, str]) -> list[MentionRecord]:
    """把每条提及的 entity_id 经 mapping 映射（不在 mapping 中的不变），保持顺序。"""
    return [replace(mention, entity_id=mapping[mention.entity_id]) if mention.entity_id in mapping else mention
            for mention in mentions]


# ---------------------------------------------------------------- 事件
def effective_events(events: Iterable[EventRecord], decisions: Iterable[Decision],
                     mapping: Mapping[str, str]) -> list[EventRecord]:
    """有效事件（保持顺序）。

    - subject_entity / object_entity 经 mapping 映射。
    - 取 latest() 中 group 为 "event"、target 等于 event_id 的决定 d；仅当 d.snapshot == (event.quote,) 时生效：
      EVENT_PASS 且原 verdict 为 REVIEW → PASS；EVENT_REJECT 且原 verdict 不是 REJECT → REJECT；其余情况忽略。
    """
    newest = latest(decisions)
    result: list[EventRecord] = []
    for event in events:
        verdict = event.verdict
        decision = newest.get(("event", event.event_id))
        if decision and decision.snapshot == (event.quote,):
            if decision.kind is DecisionKind.EVENT_PASS and event.verdict is Verdict.REVIEW:
                verdict = Verdict.PASS
            elif decision.kind is DecisionKind.EVENT_REJECT and event.verdict is not Verdict.REJECT:
                verdict = Verdict.REJECT
        subject = mapping.get(event.subject_entity, event.subject_entity)
        object_ = mapping.get(event.object_entity, event.object_entity)
        result.append(replace(event, verdict=verdict, subject_entity=subject, object_entity=object_)
                      if (verdict, subject, object_) != (event.verdict, event.subject_entity, event.object_entity)
                      else event)
    return result


# ---------------------------------------------------------------- 引用
def apply_reference_decisions(view: Mapping[str, list[dict[str, Any]]], decisions: Iterable[Decision],
                              documents: Mapping[str, str]) -> dict[str, list[dict[str, Any]]]:
    """在 build_links() 的结果上叠加引用决定，返回新的 {"links","pending","unresolved"}（不修改输入）。

    对三类中的每一行，key = reference_key(row["version_id"], row["kind"], row["reverse"], row["title"])，
    取 latest() 中 ("reference", key) 的 REFERENCE_SET 决定 d：
    - 没有 d → 原样留在原类别，加 "decided": False（新字典；candidates 列表与输入共用，调用方不得修改）。
    - d.value == "" → 进 unresolved：保留公共字段（id、kind、reverse、version_id、title、chunk_id、quote、page_start、
      page_end、section_path），加 "decided": True。
    - d.value 在 documents 中且不等于 row["version_id"] → 进 links：公共字段，加 source、target（reverse 为 False：
      source=version_id、target=d.value；True：互换）、source_title、target_title（取自 documents），"decided": True。
    - 其他（版本已不在 documents 中等）→ 决定忽略，按“没有 d”处理。
    每类结果按 id 升序。
    """
    newest = latest(decisions)
    result: dict[str, list[dict[str, Any]]] = {"links": [], "pending": [], "unresolved": []}
    common = ("id", "kind", "reverse", "version_id", "title", "chunk_id", "quote", "page_start", "page_end", "section_path")
    for category in result:
        for original in view[category]:
            row = dict(original)            # 批次 18c：浅拷贝（行内只有 candidates 是列表，这里不修改它）
            key = reference_key(row["version_id"], row["kind"], row["reverse"], row["title"])
            decision = newest.get(("reference", key))
            destination = category
            row["decided"] = False
            if decision and decision.kind is DecisionKind.REFERENCE_SET:
                if decision.value == "":
                    row = {key: row[key] for key in common}
                    row["decided"] = True
                    destination = "unresolved"
                elif decision.value in documents and decision.value != row["version_id"]:
                    row = {key: row[key] for key in common}
                    source, target = row["version_id"], decision.value
                    if row["reverse"]:
                        source, target = target, source
                    row.update(source=source, target=target, source_title=documents[source],
                               target_title=documents[target], decided=True)
                    destination = "links"
            result[destination].append(row)
    for rows in result.values():
        rows.sort(key=lambda row: row["id"])
    return result


# ---------------------------------------------------------------- 冲突
def apply_conflict_decisions(rows: Iterable[Mapping[str, Any]],
                             decisions: Iterable[Decision]) -> list[dict[str, Any]]:
    """在 build_conflicts() 的结果上叠加冲突决定（保持顺序，不修改输入）。

    取 latest() 中 ("conflict", row["id"]) 的决定 d；仅当 d.snapshot == (row["a"]["quote"], row["b"]["quote"]) 时生效：
    CONFLICT_CONFIRM → 该行 verdict 改为 "conflict"，加 "decided": True；CONFLICT_DISMISS → 去掉该行。
    没有生效的决定 → 加 "decided": False。
    """
    from copy import deepcopy

    newest = latest(decisions)
    result: list[dict[str, Any]] = []
    for original in rows:
        row = deepcopy(dict(original))
        row["decided"] = False
        decision = newest.get(("conflict", row["id"]))
        if decision and decision.snapshot == (row["a"]["quote"], row["b"]["quote"]):
            if decision.kind is DecisionKind.CONFLICT_DISMISS:
                continue
            if decision.kind is DecisionKind.CONFLICT_CONFIRM:
                row.update(verdict="conflict", decided=True)
        result.append(row)
    return result


# ---------------------------------------------------------------- 未收录问题
def pending_questions(rows: Iterable[Mapping[str, Any]], decisions: Iterable[Decision]) -> list[dict[str, Any]]:
    """未处理的未收录问题（保持顺序）：每行加 "id" = question_key(time, question)；latest() 中有 ("question", id) 的
    QUESTION_DONE 决定的行去掉。"""
    from copy import deepcopy

    newest = latest(decisions)
    result: list[dict[str, Any]] = []
    for row in rows:
        key = question_key(row["time"], row["question"])
        decision = newest.get(("question", key))
        if decision is None or decision.kind is not DecisionKind.QUESTION_DONE:
            result.append({**deepcopy(dict(row)), "id": key})
    return result


# ---------------------------------------------------------------- 创建决定
@dataclass(frozen=True)
class ReviewContext:
    """创建决定时的当前状态（全部为原始抽取结果，不是有效视图）。"""
    entities: Sequence[EntityRecord] = ()
    events: Sequence[EventRecord] = ()
    references: Sequence[ReferenceRecord] = ()
    documents: Mapping[str, str] = field(default_factory=dict)     # 已发布文档 {version_id: 标题}
    conflicts: Sequence[Mapping[str, Any]] = ()                    # build_conflicts() 的结果（原始，未叠加决定）
    questions: Sequence[Mapping[str, Any]] = ()                    # 未收录问题原始行 {"time","question","status"}
    decisions: Sequence[Decision] = ()                             # 已有决定
    relations: Sequence[RelationRecord] = ()                       # 批次 23b：原始关系（至少含目标关系）


def make_decision(kind: DecisionKind, target: str, value: str | None, *, context: ReviewContext,
                  number: int, created_at: str, actor: str = LOCAL_ACTOR) -> Decision:
    """校验并生成一条决定（不保存）。decision_id = decision_id(number)。目标不存在 → KeyError；不允许 → ReviewError。

    mapping = merge_map(context.entities, context.decisions)；名称取原始记录。
    - ENTITY_MERGE：target、value 是实体 ID。任一不在 entities → KeyError。target == value、类型不同、
      target 或 value 已被合并（在 mapping 中）→ ReviewError。snapshot ()。
      label：f"合并实体：{target.canonical} → {value.canonical}"。
    - ENTITY_DISTINCT：target 是实体 ID，不在 → KeyError；已被合并 → ReviewError。value 必须为 None。
      label：f"确认不是同一实体：{canonical}"。
    - EVENT_PASS / EVENT_REJECT：target 是 event_id，不在 events → KeyError。duplicate_of 非空 → ReviewError。
      PASS 要求原 verdict 为 REVIEW，REJECT 要求原 verdict 不是 REJECT，否则 ReviewError。value 必须为 None。
      snapshot (quote,)。label：f"通过事件：{event_label}" / f"驳回事件：{event_label}"。
    - REFERENCE_SET：target 是引用的 ref_id，在 references 中找 version_id 属于 documents 的该记录，找不到 → KeyError。
      value 必须是 "" 或 documents 中不等于记录 version_id 的版本号，否则 ReviewError。
      决定的 target 存为 reference_key(记录.version_id, 记录.kind 枚举值, 记录.reverse, 记录.title)。
      label：value 为 "" → f"引用不在库中：{title}"；否则 f"确认引用：{title} → {documents[value]}"。
    - CONFLICT_CONFIRM / CONFLICT_DISMISS：target 是冲突行 id（pair_id），不在 conflicts → KeyError。value 必须为 None。
      snapshot (a.quote, b.quote)。label：f"确认冲突：{a.title} / {b.title}" / f"不冲突：{a.title} / {b.title}"。
    - QUESTION_DONE：target 是 question_key，不在 questions → KeyError。value 必须为 None。
      label：f"已处理问题：{question}"。
    - RELATION_REJECT（批次 23b）：target 是 relation_id，不在 relations → KeyError。value 必须为 None。snapshot (quote,)。
      label：f"否决关系：{relation_label(关系, 实体规范名)}"（名称取原始实体记录，取不到用原文写法）。
    - ENTITY_SUBTYPE（批次 23b）：target 是实体 ID，不在 entities → KeyError；已被合并 → ReviewError；
      value 为 None → ReviewError；value 不是 "" 且不在 SUBTYPES[类型] 中 → ReviewError。
      label：f"修改子类：{canonical} → {value 或 “未分类”}"。
    除 ENTITY_MERGE、REFERENCE_SET、ENTITY_SUBTYPE 外，value 不为 None → ReviewError；
    ENTITY_MERGE、ENTITY_SUBTYPE 的 value 为 None → ReviewError。
    """
    id_ = decision_id(number)
    if kind not in (DecisionKind.ENTITY_MERGE, DecisionKind.REFERENCE_SET, DecisionKind.ENTITY_SUBTYPE) and value is not None:
        raise ReviewError("该操作不接受附加值")
    snapshot: tuple[str, ...] = ()
    if kind in (DecisionKind.ENTITY_MERGE, DecisionKind.ENTITY_DISTINCT):
        entities = {entity.id: entity for entity in context.entities}
        if target not in entities:
            raise KeyError(target)
        mapping = merge_map(context.entities, context.decisions)
        entity = entities[target]
        if kind is DecisionKind.ENTITY_MERGE:
            if value is None:
                raise ReviewError("请选择合并到的实体")
            if value not in entities:
                raise KeyError(value)
            if target == value:
                raise ReviewError("不能合并到自身")
            if entity.type is not entities[value].type:
                raise ReviewError("只能合并同类型实体")
            if target in mapping or value in mapping:
                raise ReviewError("实体已被合并")
            label = f"合并实体：{entity.canonical} → {entities[value].canonical}"
        else:
            if target in mapping:
                raise ReviewError("实体已被合并")
            label = f"确认不是同一实体：{entity.canonical}"
    elif kind in (DecisionKind.EVENT_PASS, DecisionKind.EVENT_REJECT):
        event = next((event for event in context.events if event.event_id == target), None)
        if event is None:
            raise KeyError(target)
        if event.duplicate_of:
            raise ReviewError("不能审核重复事件")
        if kind is DecisionKind.EVENT_PASS:
            if event.verdict is not Verdict.REVIEW:
                raise ReviewError("只能通过待审事件")
            label = f"通过事件：{event_label(event)}"
        else:
            if event.verdict is Verdict.REJECT:
                raise ReviewError("事件已被程序拒绝")
            label = f"驳回事件：{event_label(event)}"
        snapshot = (event.quote,)
    elif kind is DecisionKind.REFERENCE_SET:
        record = next((record for record in context.references
                       if record.ref_id == target and record.version_id in context.documents), None)
        if record is None:
            raise KeyError(target)
        if value != "" and (value not in context.documents or value == record.version_id):
            raise ReviewError("目标文档无效")
        target = reference_key(record.version_id, record.kind.value, record.reverse, record.title)
        label = f"引用不在库中：{record.title}" if value == "" else f"确认引用：{record.title} → {context.documents[value]}"
    elif kind in (DecisionKind.CONFLICT_CONFIRM, DecisionKind.CONFLICT_DISMISS):
        row = next((row for row in context.conflicts if row["id"] == target), None)
        if row is None:
            raise KeyError(target)
        snapshot = (row["a"]["quote"], row["b"]["quote"])
        label = ("确认冲突" if kind is DecisionKind.CONFLICT_CONFIRM else "不冲突") + f"：{row['a']['title']} / {row['b']['title']}"
    elif kind is DecisionKind.QUESTION_DONE:
        question = next((row for row in context.questions if question_key(row["time"], row["question"]) == target), None)
        if question is None:
            raise KeyError(target)
        label = f"已处理问题：{question['question']}"
    elif kind is DecisionKind.RELATION_REJECT:
        relation = next((row for row in context.relations if row.relation_id == target), None)
        if relation is None:
            raise KeyError(target)
        names = {entity.id: entity.canonical for entity in context.entities}
        snapshot = (relation.quote,)
        label = f"否决关系：{relation_label(relation, names)}"
    elif kind is DecisionKind.ENTITY_SUBTYPE:
        entity = next((row for row in context.entities if row.id == target), None)
        if entity is None:
            raise KeyError(target)
        if target in merge_map(context.entities, context.decisions):
            raise ReviewError("实体已被合并")
        if value is None:
            raise ReviewError("请选择子类")
        if value != "" and value not in SUBTYPES[entity.type]:
            raise ReviewError("子类不属于该实体的类型")
        label = f"修改子类：{entity.canonical} → {value or '未分类'}"
    else:
        raise ReviewError("不支持的审核操作")
    return Decision(id_, kind, target, value, snapshot, label, actor, created_at)
