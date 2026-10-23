"""批次 23b：实体之间的关系（纯逻辑）。规格见 docs/spec-41-entity-relations.md。

关系由模型与实体、事件在同一次调用中抽取，两端必须是本分块实体列表中的实体，必须给出逐字引文且两端名称都出现在引文中，
类型组合必须合法。通过校验即生效；审核中可以否决（RELATION_REJECT，按引文快照挂接）。
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.domain.entities import EntityType, normalize_name
from core.domain.quote_check import locate_quote

MAX_RELATIONS = 30          # 每个分块最多采纳的关系条目数，超出部分计入 dropped
MAX_ROLE_CHARS = 30
MIN_RELATION_QUOTE_CHARS = 6


class RelationType(str, Enum):
    SUPERIOR = "superior"            # 上下级：source 是 target 的上级
    MEMBER = "member"                # 分属：source 属于 target（可带职务）
    LOCATED = "located"              # 位于：source 位于 target
    PARTICIPANT = "participant"      # 参与：source 参与 target（命名事件，可带角色）
    AWARD = "award"                  # 获得：source 获得 target（荣誉）


RELATION_NAMES: dict[RelationType, str] = {
    RelationType.SUPERIOR: "上下级", RelationType.MEMBER: "分属", RelationType.LOCATED: "位于",
    RelationType.PARTICIPANT: "参与", RelationType.AWARD: "获得",
}
RELATION_LABELS: dict[str, RelationType] = {name: kind for kind, name in RELATION_NAMES.items()}

P, O, C, L, E, H = (EntityType.PERSON, EntityType.ORGANIZATION, EntityType.COUNTRY, EntityType.LOCATION,
                    EntityType.NAMED_EVENT, EntityType.HONOR)
ALLOWED: dict[RelationType, frozenset[tuple[EntityType, EntityType]]] = {
    RelationType.SUPERIOR: frozenset({(P, P), (O, O)}),
    RelationType.MEMBER: frozenset({(P, O), (P, C), (O, O), (O, C)}),
    RelationType.LOCATED: frozenset({(O, L), (O, C), (L, L), (L, C), (E, L), (E, C), (C, C)}),
    RelationType.PARTICIPANT: frozenset({(P, E), (O, E), (C, E)}),
    RelationType.AWARD: frozenset({(P, H), (O, H)}),
}
PARTICIPANT_ROLES = ("发起者", "主导者", "背后力量", "参与者")

PROMPT_RULES = (
    "3. relations：entities 中的实体两两之间、正文明确写出的关系。type 只能是：\n"
    "   上下级（source 是 target 的上级：领导与下属、母公司与子公司、上级机关与下属机构）；\n"
    "   分属（source 属于 target：人物是组织的成员或任职、人物的国籍、组织隶属于上级组织或所属国家；"
    "role 写原文中的职务或军衔，如“首席财务官”“上校”，没有写空字符串）；\n"
    "   位于（source 位于 target：组织总部、地点、命名事件的举办地）；\n"
    "   参与（source 参与命名事件或项目 target；role 取 发起者、主导者、背后力量、参与者 之一）；\n"
    "   获得（人物或组织 source 获得荣誉 target）。\n"
    "   source、target 必须与 entities 中的 name 完全相同；time 照抄原文中的时间（如“2025年起”），没有写空字符串；"
    "quote 是从正文逐字摘抄的一句原文，必须同时包含 source 和 target。只凭推测、正文没有写明的关系不要输出。"
)


@dataclass(frozen=True)
class ExtractedRelation:
    kind: RelationType
    source: str                     # 实体列表中的名称（原样）
    source_type: EntityType
    target: str
    target_type: EntityType
    role: str | None
    time_expr: str | None
    quote: str
    start: int                      # 引文在分块正文中的位置
    end: int


def parse_relations(items: Any, listed: Mapping[str, EntityType], source_text: str) -> tuple[list[ExtractedRelation], int]:
    """解析模型给出的 relations。listed：本分块实体列表 {normalize_name(名称): 类型}（只含被采纳的实体）。
    items 不是列表 → ([], 0)（旧模型输出没有 relations 时不算错）。前 MAX_RELATIONS 条逐条检查，其余每条 dropped += 1。
    一条有效需满足：是 dict；type、source、target、quote 是字符串，role、time 缺失、为 None 或字符串；
    type 在 RELATION_LABELS 中；source、target 规范化后都在 listed 中且不相同；(source 类型, target 类型) 在 ALLOWED[kind] 中；
    locate_quote(quote, source_text, min_chars=MIN_RELATION_QUOTE_CHARS) 命中，且 source、target 都能在引文中定位
    （locate_quote(name, quote, min_chars=2)）。否则 dropped += 1。
    role：去空白；超过 MAX_ROLE_CHARS → 截断；参与关系的 role 不在 PARTICIPANT_ROLES 中 → "参与者"；
    上下级、位于、获得没有 role（一律 None）；空串 → None。time：去空白，空串 → None。
    (kind, source 规范名, target 规范名) 与已采纳的重复 → 跳过，不计 dropped。"""
    if not isinstance(items, list):
        return [], 0
    dropped = max(0, len(items) - MAX_RELATIONS)
    result: list[ExtractedRelation] = []
    seen: set[tuple[RelationType, str, str]] = set()
    for item in items[:MAX_RELATIONS]:
        relation = _parse_one(item, listed, source_text)
        if relation is None:
            dropped += 1
            continue
        key = (relation.kind, normalize_name(relation.source), normalize_name(relation.target))
        if key not in seen:
            seen.add(key)
            result.append(relation)
    return result, dropped


def _parse_one(item: Any, listed: Mapping[str, EntityType], source_text: str) -> ExtractedRelation | None:
    if (not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("type", "source", "target", "quote"))
            or not all(item.get(key) is None or isinstance(item[key], str) for key in ("role", "time"))):
        return None
    kind = RELATION_LABELS.get(item["type"].strip())
    source, target, quote = item["source"].strip(), item["target"].strip(), item["quote"].strip()
    source_key, target_key = normalize_name(source), normalize_name(target)
    if kind is None or not source_key or not target_key or source_key == target_key:
        return None
    if source_key not in listed or target_key not in listed:
        return None
    types = (listed[source_key], listed[target_key])
    if types not in ALLOWED[kind]:
        return None
    match = locate_quote(quote, source_text, min_chars=MIN_RELATION_QUOTE_CHARS) if quote else None
    if match is None or any(locate_quote(name, quote, min_chars=2) is None for name in (source, target)):
        return None
    role = (item.get("role") or "").strip()[:MAX_ROLE_CHARS] or None
    if kind is RelationType.PARTICIPANT:
        role = role if role in PARTICIPANT_ROLES else "参与者"
    elif kind is not RelationType.MEMBER:
        role = None
    time_expr = (item.get("time") or "").strip() or None
    return ExtractedRelation(kind, source, types[0], target, types[1], role, time_expr, quote, match.start, match.end)


@dataclass(frozen=True)
class RelationRecord:
    relation_id: str                # f"{version_id}:l{序号:04d}"，序号在一个版本内从 1 起
    version_id: str
    chunk_id: str
    kind: RelationType
    source_entity: str
    target_entity: str
    source_name: str                # 原文写法
    target_name: str
    role: str | None
    time_expr: str | None
    quote: str
    quote_start: int
    quote_end: int
    page_start: int | None = None
    page_end: int | None = None
    section_path: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {**vars(self), "kind": self.kind.value}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RelationRecord:
        values = {key: data[key] for key in cls.__dataclass_fields__}
        values["kind"] = RelationType(values["kind"])
        return cls(**values)


def relation_label(relation: RelationRecord, names: Mapping[str, str]) -> str:
    """“A 上下级 B”形式的说明；names 取不到时用原文写法。分属带职务时为“A 分属 B（职务）”。"""
    text = (f"{names.get(relation.source_entity, relation.source_name)} {RELATION_NAMES[relation.kind]} "
            f"{names.get(relation.target_entity, relation.target_name)}")
    return f"{text}（{relation.role}）" if relation.role else text


def relation_edges(relations: Iterable[RelationRecord], entity_ids: Iterable[str]) -> list[dict[str, Any]]:
    """图谱上的关系边（只连两端都在 entity_ids 中、且两端不同的关系），按 (kind, source, target) 聚合：
    {"id": f"r:{kind}:{source}:{target}", "source": "ent:"+source, "target": "ent:"+target, "kind": "relation",
     "relation": kind 枚举值, "label": 中文名, "count": 条数, "roles": 非空 role 去重升序}，按 id 升序。"""
    kept = set(entity_ids)
    counts: Counter[tuple[str, str, str]] = Counter()
    roles: dict[tuple[str, str, str], set[str]] = {}
    for relation in relations:
        if relation.source_entity in kept and relation.target_entity in kept and relation.source_entity != relation.target_entity:
            key = (relation.kind.value, relation.source_entity, relation.target_entity)
            counts[key] += 1
            if relation.role:
                roles.setdefault(key, set()).add(relation.role)
    edges = [{"id": f"r:{kind}:{source}:{target}", "source": "ent:" + source, "target": "ent:" + target,
              "kind": "relation", "relation": kind, "label": RELATION_NAMES[RelationType(kind)], "count": count,
              "roles": sorted(roles.get((kind, source, target), ()))}
             for (kind, source, target), count in counts.items()]
    return sorted(edges, key=lambda edge: edge["id"])


def entity_relation_rows(entity_id: str, relations: Iterable[RelationRecord], entities: Mapping[str, Any],
                         titles: Mapping[str, str], *, limit: int) -> tuple[list[dict[str, Any]], bool]:
    """实体详情中的关系列表：一端为 entity_id、另一端在 entities（{id: EntityRecord}）中的关系。
    每行 {"relation_id","kind","label","direction"("out"：本实体是 source；"in"：是 target),
          "other": {"id","canonical","type","subtype"}, "role","time_expr","quote","version_id","title",
          "chunk_id","page_start","page_end","section_path"}；
    按 (kind, direction, other.canonical, relation_id) 升序，前 limit 条；第二个返回值表示是否截掉。"""
    rows = []
    for relation in relations:
        if relation.source_entity == entity_id:
            direction, other_id = "out", relation.target_entity
        elif relation.target_entity == entity_id:
            direction, other_id = "in", relation.source_entity
        else:
            continue
        other = entities.get(other_id)
        if other is None or other_id == entity_id:
            continue
        rows.append({"relation_id": relation.relation_id, "kind": relation.kind.value,
                     "label": RELATION_NAMES[relation.kind], "direction": direction,
                     "other": {"id": other.id, "canonical": other.canonical, "type": other.type.value,
                               "subtype": other.subtype},
                     "role": relation.role, "time_expr": relation.time_expr, "quote": relation.quote,
                     "version_id": relation.version_id, "title": titles.get(relation.version_id, relation.version_id),
                     "chunk_id": relation.chunk_id, "page_start": relation.page_start, "page_end": relation.page_end,
                     "section_path": relation.section_path})
    rows.sort(key=lambda row: (row["kind"], row["direction"], row["other"]["canonical"], row["relation_id"]))
    return rows[:limit], len(rows) > limit


def scope_relations(relations: Iterable[RelationRecord], visible: Sequence[str] | set[str]) -> list[RelationRecord]:
    """只保留 version_id 在可读集合中的关系（保持顺序）。"""
    allowed = set(visible)
    return [relation for relation in relations if relation.version_id in allowed]
