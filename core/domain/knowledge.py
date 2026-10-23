"""知识记录（实体、提及、事件）及时间轴、图谱视图（纯逻辑）。规格：docs/spec-16-extraction.md、docs/spec-17-graph-timeline.md

记录由用例层 core.app.extract 生成，由界面层保存为 JSON；视图函数把记录组装成前端直接使用的结构。
"""
from __future__ import annotations

import re
from collections import Counter, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any

from core.domain.dates import Issue, Precision
from core.domain.entities import Entity, EntityType
from core.domain.events import Verdict
from core.domain.taxonomy import normalize_subtype, parse_type

ENTITY_ID = re.compile(r"ent_[0-9]{5,}")


def entity_id(number: int) -> str:
    """第 number 个实体的 ID："ent_" + 至少 5 位的十进制序号（ent_00001、ent_123456）。number < 1 → ValueError。"""
    if number < 1:
        raise ValueError("实体序号必须为正整数")
    return f"ent_{number:05d}"


def entity_number(value: str) -> int:
    """entity_id 的逆运算。不符合 ENTITY_ID 全匹配 → ValueError。"""
    if ENTITY_ID.fullmatch(value) is None:
        raise ValueError("实体 ID 格式无效")
    return int(value[4:])


@dataclass(frozen=True)
class EntityRecord:
    id: str
    type: EntityType
    canonical: str                                  # 首次出现时的写法
    aliases: tuple[str, ...] = ()                   # 人工确认的别名（本批不产生，预留）
    ambiguous: bool = False                         # 人工标记同名多义（本批不产生，预留）
    review_candidates: tuple[str, ...] = ()         # 新建时 resolve 给出的“可能是同一实体”的候选 ID；空 = 无需确认
    subtype: str | None = None                      # 批次 23a：子类（taxonomy.SUBTYPES[type] 之一），未分类为 None

    def to_entity(self) -> Entity:
        """转换为 core.domain.entities.Entity（aliases 转 frozenset）。"""
        return Entity(self.id, self.type, self.canonical, frozenset(self.aliases), self.ambiguous)

    def to_json(self) -> dict[str, Any]:
        """{"id","type"(枚举值),"canonical","aliases"(list),"ambiguous","review_candidates"(list),"subtype"}。"""
        return {"id": self.id, "type": self.type.value, "canonical": self.canonical,
                "aliases": list(self.aliases), "ambiguous": self.ambiguous,
                "review_candidates": list(self.review_candidates), "subtype": self.subtype}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> EntityRecord:
        """to_json 的逆运算；缺少键或枚举值无效 → KeyError / ValueError。
        批次 23a：旧数据没有 subtype 时为 None；旧类型值按 taxonomy.parse_type 换算（"media" → 组织、子类“媒体”）。
        子类不在该类型的列表中 → None。"""
        type_, implied = parse_type(data["type"])
        subtype = normalize_subtype(type_, data.get("subtype")) or implied
        return cls(data["id"], type_, data["canonical"], tuple(data["aliases"]),
                   data["ambiguous"], tuple(data["review_candidates"]), subtype)


@dataclass(frozen=True)
class MentionRecord:
    version_id: str
    chunk_id: str
    entity_id: str
    name: str                    # 原文写法
    start: int                   # 在分块正文（去面包屑后）中的位置
    end: int
    role: str                    # "entity"（实体列表）| "subject" | "object"（事件参与者）

    def to_json(self) -> dict[str, Any]:
        """字段名与属性名相同的扁平字典。"""
        return dict(vars(self))

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> MentionRecord:
        return cls(**{key: data[key] for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class EventRecord:
    event_id: str                        # f"{version_id}:e{序号:04d}"，序号在一个版本内从 1 起，含被丢弃的事件
    version_id: str
    chunk_id: str
    verdict: Verdict
    problems: tuple[str, ...]            # Problem 枚举值，顺序同 EventCheck.problems
    subject: str
    action: str
    object: str | None
    time_expr: str | None
    place: str | None
    quote: str
    subject_type: EntityType | None = None
    object_type: EntityType | None = None
    subject_entity: str | None = None    # 对齐后的实体 ID
    object_entity: str | None = None
    date_start: date | None = None
    date_end: date | None = None
    precision: Precision | None = None
    date_issue: Issue | None = None
    quote_start: int | None = None       # 引文在分块正文中的位置
    quote_end: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    section_path: str | None = None
    fingerprint: str | None = None
    duplicate_of: str | None = None      # 同一版本内指纹相同的第一条事件的 event_id

    def to_json(self) -> dict[str, Any]:
        """扁平字典，键名同属性名。枚举写枚举值、日期写 ISO 字符串（YYYY-MM-DD）、tuple 写 list、None 写 null。"""
        result: dict[str, Any] = {}
        for key, value in vars(self).items():
            if isinstance(value, Enum):
                value = value.value
            elif isinstance(value, date):
                value = value.isoformat()
            elif isinstance(value, tuple):
                value = list(value)
            result[key] = value
        return result

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> EventRecord:
        """to_json 的逆运算。"""
        values = {key: data[key] for key in cls.__dataclass_fields__}
        values["verdict"] = Verdict(values["verdict"])
        values["problems"] = tuple(values["problems"])
        for key in ("subject_type", "object_type"):
            if values[key] is not None:
                values[key] = parse_type(values[key])[0]          # 批次 23a：旧值 "media" → 组织
        for key in ("date_start", "date_end"):
            if values[key] is not None:
                values[key] = date.fromisoformat(values[key])
        if values["precision"] is not None:
            values["precision"] = Precision(values["precision"])
        if values["date_issue"] is not None:
            values["date_issue"] = Issue(values["date_issue"])
        return cls(**values)


def visible(event: EventRecord, *, include_review: bool) -> bool:
    """视图中是否展示该事件：REJECT 不展示；duplicate_of 非空不展示；REVIEW 仅当 include_review 为 True 时展示。"""
    return (event.verdict is not Verdict.REJECT and not event.duplicate_of
            and (event.verdict is not Verdict.REVIEW or include_review))


def event_label(event: EventRecord) -> str:
    """主体、行为、对象中非空者用一个空格连接，例如 "华东物流有限公司 签订 采购合同"。"""
    return " ".join(value for value in (event.subject, event.action, event.object) if value)


def build_timeline(events: Iterable[EventRecord], titles: Mapping[str, str], *,
                   include_review: bool = True) -> dict[str, list[dict[str, Any]]]:
    """时间轴数据：{"items": [...], "undated": [...]}。

    只取 visible(e, include_review=include_review) 的事件。date_start 非空的进 items，按 (start, end, id) 升序；
    date_start 为空的进 undated，按 id 升序。每一项是字典：
      id、label(event_label)、verdict(枚举值)、start/end(ISO 日期，end 含当天；undated 中为 None)、precision(枚举值或 None)、
      version_id、title(titles.get(version_id, version_id))、chunk_id、subject、action、object、time_expr、place、quote、
      page_start、page_end、section_path、problems(list)。
    """
    items: list[dict[str, Any]] = []
    undated: list[dict[str, Any]] = []
    for event in events:
        if not visible(event, include_review=include_review):
            continue
        row = {"id": event.event_id, "label": event_label(event), "verdict": event.verdict.value,
               "start": event.date_start.isoformat() if event.date_start else None,
               "end": event.date_end.isoformat() if event.date_start and event.date_end else None,
               "precision": event.precision.value if event.precision else None,
               "version_id": event.version_id, "title": titles.get(event.version_id, event.version_id),
               "chunk_id": event.chunk_id, "subject": event.subject, "action": event.action,
               "object": event.object, "time_expr": event.time_expr, "place": event.place, "quote": event.quote,
               "page_start": event.page_start, "page_end": event.page_end, "section_path": event.section_path,
               "problems": list(event.problems)}
        (items if event.date_start else undated).append(row)
    items.sort(key=lambda row: (row["start"], row["end"], row["id"]))
    undated.sort(key=lambda row: row["id"])
    return {"items": items, "undated": undated}


def build_graph(documents: Mapping[str, str], entities: Iterable[EntityRecord], mentions: Iterable[MentionRecord],
                events: Iterable[EventRecord], *, include_review: bool = True, include_events: bool = True,
                focus: str | None = None, depth: int = 1,
                doc_edges: Iterable[Mapping[str, Any]] = ()) -> dict[str, list[dict[str, Any]]]:
    """图谱数据：{"nodes": [...], "edges": [...]}。documents 为 {version_id: 标题}，只含要展示的（已发布）文档。

    节点 ID：文档 "doc:<version_id>"、实体 "ent:<entity_id>"、事件 "evt:<event_id>"。
    边（只保留两端节点都存在的边；version_id 不在 documents 中的提及和事件一律忽略）：
    - "mentions"：文档 → 实体。每个 (version_id, entity_id) 一条，id "m:<version_id>:<entity_id>"，
      weight = 该文档中指向该实体的 MentionRecord 条数（不论 role）。entity_id 不在 entities 中的提及忽略。
    - include_events 为 True 时，对每个 visible(e, include_review=include_review) 的事件：
      "contains"：文档 → 事件，id "c:<event_id>"；
      "subject"：事件 → 实体，id "s:<event_id>"（subject_entity 非空且在 entities 中）；
      "object"：事件 → 实体，id "o:<event_id>"（object_entity 非空且在 entities 中）。
      这三种边 weight 为 1。
    节点：
    - 文档：documents 中的每一个都出现（即使没有边）。{"id","kind":"document","label":标题,"version_id"}
    - 实体：至少有一条边才出现。{"id","kind":实体类型枚举值,"label":canonical,"entity_id","needs_review":review_candidates 非空}
    - 事件：随 "contains" 边出现。{"id","kind":"event","label":event_label,"event_id","verdict","start"(ISO 或 None),"version_id"}
    focus 不为 None 时：focus 必须是上述完整图中的节点 ID，否则 ValueError；depth 必须为 1–3，否则 ValueError
    （depth 只在 focus 给出时检查）。只保留与 focus 无向距离 ≤ depth 的节点，以及两端都保留的边。
    最后为每个节点加 "degree"（保留下来的边中与它相连的条数）。nodes 按 id 升序，edges 按 id 升序。
    边的字典：{"id","source","target","kind","weight"}。
    """
    by_id = {entity.id: entity for entity in entities}
    nodes = {f"doc:{version}": {"id": f"doc:{version}", "kind": "document", "label": title, "version_id": version}
             for version, title in documents.items()}
    edges: list[dict[str, Any]] = []
    counts = Counter((mention.version_id, mention.entity_id) for mention in mentions
                     if mention.version_id in documents and mention.entity_id in by_id)

    def _entity_node(id_: str) -> str:
        entity = by_id[id_]
        node_id = f"ent:{id_}"
        nodes[node_id] = {"id": node_id, "kind": entity.type.value, "label": entity.canonical,
                          "entity_id": id_, "needs_review": bool(entity.review_candidates)}
        return node_id

    for (version, id_), count in counts.items():
        edges.append({"id": f"m:{version}:{id_}", "source": f"doc:{version}", "target": _entity_node(id_),
                      "kind": "mentions", "weight": count})
    if include_events:
        for event in events:
            if event.version_id not in documents or not visible(event, include_review=include_review):
                continue
            id_ = f"evt:{event.event_id}"
            nodes[id_] = {"id": id_, "kind": "event", "label": event_label(event), "event_id": event.event_id,
                          "verdict": event.verdict.value, "start": event.date_start.isoformat() if event.date_start else None,
                          "version_id": event.version_id}
            edges.append({"id": f"c:{event.event_id}", "source": f"doc:{event.version_id}", "target": id_,
                          "kind": "contains", "weight": 1})
            for prefix, kind, target in (("s", "subject", event.subject_entity), ("o", "object", event.object_entity)):
                if target in by_id:
                    edges.append({"id": f"{prefix}:{event.event_id}", "source": id_, "target": _entity_node(target),
                                  "kind": kind, "weight": 1})
    for edge in doc_edges:
        source, target = f"doc:{edge['source']}", f"doc:{edge['target']}"
        if source in nodes and target in nodes:
            edges.append({**edge, "source": source, "target": target})
    if focus is not None:
        if focus not in nodes or not 1 <= depth <= 3:
            raise ValueError("中心节点不存在或深度不在 1 至 3 之间")
        adjacency: dict[str, set[str]] = {id_: set() for id_ in nodes}
        for edge in edges:
            adjacency[edge["source"]].add(edge["target"])
            adjacency[edge["target"]].add(edge["source"])
        retained = {focus}
        queue = deque([(focus, 0)])
        while queue:
            current, distance = queue.popleft()
            if distance == depth:
                continue
            for neighbor in adjacency[current] - retained:
                retained.add(neighbor)
                queue.append((neighbor, distance + 1))
        nodes = {id_: node for id_, node in nodes.items() if id_ in retained}
        edges = [edge for edge in edges if edge["source"] in retained and edge["target"] in retained]
    degrees = Counter(endpoint for edge in edges for endpoint in (edge["source"], edge["target"]))
    return {"nodes": [{**nodes[id_], "degree": degrees[id_]} for id_ in sorted(nodes)],
            "edges": sorted(edges, key=lambda edge: edge["id"])}


# ---------------------------------------------------------------- 批次 15b：按窗口与数量取数
TIMELINE_LIMIT_MAX = 5000
DETAIL_LIMIT = 500


def _overlaps(row: Mapping[str, Any], start: date | None, end: date | None) -> bool:
    first = date.fromisoformat(row["start"])
    last = date.fromisoformat(row["end"]) if row["end"] else first
    return (end is None or first <= end) and (start is None or last >= start)


def timeline_window(events: Iterable[EventRecord], titles: Mapping[str, str], *, include_review: bool = True,
                    start: date | None = None, end: date | None = None, limit: int = 2000) -> dict[str, Any]:
    """时间轴的一个窗口：{"items": [...], "undated": [...], "truncated": bool}。行的格式同 build_timeline。

    - limit 不在 1–TIMELINE_LIMIT_MAX、或 start 与 end 都给出且 start > end → ValueError。
    - items：build_timeline 的 items 中与 [start, end] 有交集的（事件区间为 [start, end or start]，两端含当天；
      start/end 为 None 表示不限）。超过 limit 条时保留排序最靠后（最近）的 limit 条，仍按原顺序升序。
    - undated：start 与 end 都为 None 时为 build_timeline 的 undated 的前 limit 条；给出任一边界时为 []。
    - truncated：items 或 undated 有被截掉的。
    """
    if not 1 <= limit <= TIMELINE_LIMIT_MAX:
        raise ValueError("时间轴条数上限超出范围")
    if start is not None and end is not None and start > end:
        raise ValueError("时间范围起点晚于终点")
    base = build_timeline(events, titles, include_review=include_review)
    items = [row for row in base["items"] if _overlaps(row, start, end)]
    undated = base["undated"] if start is None and end is None else []
    return {"items": items[-limit:], "undated": undated[:limit],
            "truncated": len(items) > limit or len(undated) > limit}


def entity_detail(entity: EntityRecord, entities: Mapping[str, EntityRecord], mention_counts: Mapping[str, int],
                  events: Iterable[EventRecord], titles: Mapping[str, str], *,
                  redirected_from: str | None = None, limit: int = DETAIL_LIMIT,
                  mention_total: int | None = None) -> dict[str, Any]:
    """实体详情（/api/entities/{id} 的响应体）。entity、entities、events 均为有效视图（已叠加审核决定）。

    - mention_counts：{version_id: 有效提及中 entity_id 等于 entity.id 的条数}（不论文档是否发布；计数为 0 的版本不应出现）。
      调用方可以只给版本号最小的 limit + 1 个版本，同时用 mention_total 给出全部版本的总条数（结果相同）。
    - mentions：mention_total；为 None 时取 mention_counts 的总和。
    - documents：按 version_id 分组的 [{"version_id","title"(titles.get 取不到用版本号),"count"}]，按版本号升序，
      最多 limit 个；documents_truncated 表示是否截掉。
    - events：subject_entity 或 object_entity 等于 entity.id 的事件，经 build_timeline（include_review=True）后，
      先放 items 中排序最靠后的至多 limit 条（保持升序），再用 undated 按顺序补足到 limit 条；events_truncated 表示是否截掉。
    - candidates：entity.review_candidates 中在 entities 里的，[{"id","canonical","type"}]，保持顺序。
    - entity：entity.to_json()。redirected_from 不为 None 时加同名键。
    """
    counts = {version: count for version, count in mention_counts.items() if count}
    timeline = build_timeline((event for event in events if entity.id in (event.subject_entity, event.object_entity)),
                              titles)
    dated = timeline["items"][-limit:]
    listed = dated + timeline["undated"][:limit - len(dated)]
    versions = sorted(counts)
    data: dict[str, Any] = {
        "entity": entity.to_json(), "mentions": sum(counts.values()) if mention_total is None else mention_total,
        "documents": [{"version_id": version, "title": titles.get(version, version), "count": counts[version]}
                      for version in versions[:limit]],
        "documents_truncated": len(versions) > limit,
        "events": listed, "events_truncated": len(timeline["items"]) + len(timeline["undated"]) > len(listed),
        "candidates": [{"id": id_, "canonical": entities[id_].canonical, "type": entities[id_].type.value}
                       for id_ in entity.review_candidates if id_ in entities]}
    if redirected_from is not None:
        data["redirected_from"] = redirected_from
    return data
