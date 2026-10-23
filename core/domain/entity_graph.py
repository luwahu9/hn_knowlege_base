"""实体关系图（纯逻辑）：不含文档节点，实体之间的关系来自业务事件。规格：docs/spec-22-ui-redesign.md

结构：主体实体 —(动作)→ 事件 —(动作)→ 对象实体。每条边对应一个事件，证据就是该事件的引文。
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Container, Iterable, Mapping
from typing import Any

from core.domain.entity_relations import RelationRecord, relation_edges
from core.domain.knowledge import EntityRecord, EventRecord, MentionRecord, event_label, visible

MIN_LIMIT, MAX_LIMIT = 20, 1000


def event_node(event: EventRecord, title: str) -> dict[str, Any]:
    """事件节点字典（见 build_entity_graph）。title 为事件所在文档的标题。"""
    return {"id": "evt:" + event.event_id, "kind": "event", "label": event_label(event),
            "event_id": event.event_id, "verdict": event.verdict.value,
            "start": event.date_start.isoformat() if event.date_start else None,
            "version_id": event.version_id, "title": title,
            "quote": event.quote, "chunk_id": event.chunk_id, "page_start": event.page_start,
            "page_end": event.page_end, "section_path": event.section_path, "problems": list(event.problems)}


def event_edges(event: EventRecord, entity_ids: Container[str]) -> list[dict[str, Any]]:
    """事件的边（见 build_entity_graph）：参与者不在 entity_ids 中的那一侧不生成边。顺序：主体边在前。"""
    edges = []
    node_id = "evt:" + event.event_id
    for prefix, kind, id_ in (("s", "subject", event.subject_entity), ("o", "object", event.object_entity)):
        if id_ is None or id_ not in entity_ids:
            continue
        entity_node_id = "ent:" + id_
        source, target = (entity_node_id, node_id) if kind == "subject" else (node_id, entity_node_id)
        edges.append({"id": prefix + ":" + event.event_id, "source": source, "target": target,
                      "kind": kind, "label": event.action, "verdict": event.verdict.value, "event_id": event.event_id})
    return edges


def entity_node(entity: EntityRecord, mentions: int, documents: int) -> dict[str, Any]:
    """实体节点字典（见 build_entity_graph）。mentions、documents 为已发布文档中的提及条数与涉及文档数。"""
    return {"id": "ent:" + entity.id, "kind": entity.type.value, "subtype": entity.subtype, "label": entity.canonical,
            "entity_id": entity.id, "aliases": list(entity.aliases), "needs_review": bool(entity.review_candidates),
            "mentions": mentions, "documents": documents}


def assemble_entity_graph(nodes: Mapping[str, dict[str, Any]], edges: Iterable[dict[str, Any]], kept: Collection[str], *,
                          total_nodes: int, truncated: bool, total_complete: bool = True) -> dict[str, Any]:
    """按保留的节点 ID 组装最终结果：只保留两端都保留的边，节点加 degree（保留边中的条数），nodes、edges 按 id 升序。
    nodes 必须包含 kept 中的全部节点（可以更多）。"""
    kept = set(kept)
    edges = [edge for edge in edges if edge["source"] in kept and edge["target"] in kept]
    degrees = Counter(endpoint for edge in edges for endpoint in (edge["source"], edge["target"]))
    return {"nodes": [dict(nodes[id_], degree=degrees[id_]) for id_ in sorted(kept)],
            "edges": sorted(edges, key=lambda edge: edge["id"]), "total_nodes": total_nodes, "truncated": truncated,
            "total_complete": total_complete}


def build_entity_graph(documents: Mapping[str, str], entities: Iterable[EntityRecord], mentions: Iterable[MentionRecord],
                       events: Iterable[EventRecord], *, include_review: bool = True, focus: str | None = None,
                       depth: int = 1, limit: int = 300,
                       relations: Iterable[RelationRecord] = ()) -> dict[str, Any]:
    """实体关系图：{"nodes": [...], "edges": [...], "total_nodes": int, "truncated": bool, "total_complete": bool}。

    documents 为 {version_id: 标题}，只含已发布文档；version_id 不在其中的事件和提及一律忽略。
    事件：取 visible(e, include_review=include_review) 的事件。每个事件生成一个事件节点（event_node），以及 event_edges
    给出的边：主体边 {"id": "s:<event_id>", "source": "ent:<主体>", "target": "evt:<event_id>", "kind": "subject"}、
    对象边 {"id": "o:<event_id>", "source": "evt:<event_id>", "target": "ent:<对象>", "kind": "object"}，
    参与者不在 entities 中的一侧没有边；两边都没有的事件不生成节点。边另有 "label"（action）、"verdict"、"event_id"。
    实体节点（entity_node）至少有一条边才出现；mentions/documents 为已发布文档中指向它的提及条数与不同文档数。
    完整图中每个节点的度数 = 与它相连的边数。
    批次 23b-r2：relations 为有效关系（调用方已做否决、合并映射与可读范围）。version_id 在 documents 中、两端都在 entities 中的
    关系经 relation_edges 聚合成关系边（同方向同类型一条），与事件边一样参与构图：端点成为实体节点，计入度数、截断排序与
    局部图的邻接展开。只有关系边的实体也会出现。

    focus 为 None（全图）：节点数 > limit 时按 (度数降序, 节点 ID 升序) 保留前 limit 个，truncated 为 True；
      total_nodes 为截断前的节点数，total_complete 为 True。
    focus 不为 None（局部图，批次 15b 起也受 limit 约束）：focus 必须是完整图中的节点 ID，depth 必须为 1–3，否则 ValueError。
      按无向距离分层：L0 = {focus}，Lk = 与 L(k-1) 相邻且不在更浅层中的节点。从 L1 起逐层展开到 depth 层为止；
      若展开到第 d 层后累计节点数（L0..Ld）≥ limit，则不再展开更深的层。
      total_nodes = 已展开各层的节点总数；total_complete = 是否展开到了 depth 层（提前停止为 False，
      恰好在第 depth 层停止也算 True）；truncated = total_nodes > limit；
      保留 L0..L(d-1) 的全部节点，以及第 d 层（最后展开的一层）中按节点 ID 升序的前 (limit − 前面各层节点数) 个。
    limit 不在 MIN_LIMIT–MAX_LIMIT → ValueError（在任何处理之前检查）。
    最后由 assemble_entity_graph 组装：只保留两端都保留的边；每个节点加 "degree"（保留边中的条数）；nodes、edges 按 id 升序。
    """
    if not MIN_LIMIT <= limit <= MAX_LIMIT:
        raise ValueError("节点上限超出范围")
    if focus is not None and not 1 <= depth <= 3:
        raise ValueError("局部图深度超出范围")
    entity_map = {entity.id: entity for entity in entities}
    counts: Counter[str] = Counter()
    mentioned_documents: dict[str, set[str]] = {}
    for mention in mentions:
        if mention.version_id in documents:
            counts[mention.entity_id] += 1
            mentioned_documents.setdefault(mention.entity_id, set()).add(mention.version_id)
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    participants: set[str] = set()
    for event in events:
        if event.version_id not in documents or not visible(event, include_review=include_review):
            continue
        linked = event_edges(event, entity_map)
        if not linked:
            continue
        nodes["evt:" + event.event_id] = event_node(event, documents[event.version_id])
        edges.extend(linked)
        participants.update(id_ for id_ in (event.subject_entity, event.object_entity) if id_ in entity_map)
    linked_relations = relation_edges((r for r in relations if r.version_id in documents), entity_map)
    edges.extend(linked_relations)
    participants.update(edge[side][4:] for edge in linked_relations for side in ("source", "target"))
    for id_ in participants:
        nodes["ent:" + id_] = entity_node(entity_map[id_], counts[id_], len(mentioned_documents.get(id_, set())))
    neighbors: dict[str, set[str]] = {id_: set() for id_ in nodes}
    degrees: Counter[str] = Counter()
    for edge in edges:
        source, target = edge["source"], edge["target"]
        neighbors[source].add(target)
        neighbors[target].add(source)
        degrees.update((source, target))
    if focus is None:
        total_nodes = len(nodes)
        truncated = total_nodes > limit
        kept = sorted(nodes, key=lambda id_: (-degrees[id_], id_))[:limit] if truncated else list(nodes)
        return assemble_entity_graph(nodes, edges, kept, total_nodes=total_nodes, truncated=truncated)
    if focus not in nodes:
        raise ValueError("未找到图谱节点")
    levels = [[focus]]
    seen = {focus}
    while len(levels) <= depth and len(seen) < limit:
        layer = sorted({neighbor for node_id in levels[-1] for neighbor in neighbors[node_id]} - seen)
        if not layer:
            break
        levels.append(layer)
        seen.update(layer)
    total_nodes = len(seen)
    total_complete = len(levels) > depth or len(seen) < limit
    kept = [id_ for layer in levels[:-1] for id_ in layer]
    kept += levels[-1][:limit - len(kept)]
    return assemble_entity_graph(nodes, edges, kept, total_nodes=total_nodes, truncated=total_nodes > limit,
                                 total_complete=total_complete)
