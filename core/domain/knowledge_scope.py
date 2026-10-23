"""按可读文档裁剪知识数据（批次 16b）。规格见 docs/spec-29-scoped-views.md。纯逻辑。

裁剪规则（对“已叠加审核决定的有效视图”进行）：
- 提及、事件：只保留 version_id 在可读集合中的；
- 实体：只保留被保留的提及或事件（subject_entity / object_entity）引用到的；其 review_candidates 只保留同样被保留的实体；
- 实体的 canonical（首次出现时的写法）原样保留——同一实体在可读文档中也出现过，名称本身不视为越权（规格第二节）。
"""
from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import replace

from core.domain.knowledge import EntityRecord, EventRecord, MentionRecord


def scope_records(entities: Iterable[EntityRecord], mentions: Iterable[MentionRecord], events: Iterable[EventRecord],
                  visible: Collection[str]) -> tuple[list[EntityRecord], list[MentionRecord], list[EventRecord]]:
    """返回裁剪后的 (实体, 提及, 事件)，各自保持原有顺序。"""
    kept_mentions = [mention for mention in mentions if mention.version_id in visible]
    kept_events = [event for event in events if event.version_id in visible]
    referenced = {mention.entity_id for mention in kept_mentions}
    for event in kept_events:
        referenced.update(id_ for id_ in (event.subject_entity, event.object_entity) if id_)
    kept_entities = [replace(entity, review_candidates=tuple(id_ for id_ in entity.review_candidates
                                                             if id_ in referenced))
                     for entity in entities if entity.id in referenced]
    return kept_entities, kept_mentions, kept_events
