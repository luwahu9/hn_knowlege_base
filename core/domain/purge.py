"""彻底删除的纯逻辑（批次 20b）。规格见 docs/spec-37-purge.md。

用户决定（2026-10-21）：登记处保留文件名、标题与删除信息；派生内容（无人提及的实体名称、相关审核决定的引文与说明）一并清除；
对话中引用了被删文档的回答从记录中抹去，只留问题。
"""
from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import replace
from typing import Any

from core.domain.knowledge import EntityRecord, EventRecord, MentionRecord
from core.domain.review import GROUPS, Decision

PURGED = "purged"
PURGEABLE = frozenset({"withdrawn", "superseded"})
TOMBSTONE_KEYS = ("version_id", "folder", "filename", "title", "imported_at", "origin")
PURGED_ANSWER = "（这一轮引用的文档已被彻底删除，回答已抹去。）"
SCRUBBED_LABEL = "（相关文档已彻底删除）"
_SHA256 = re.compile(r"[0-9a-f]{64}")


def tombstone(record: Mapping[str, Any], entry: Mapping[str, Any]) -> dict[str, Any]:
    """删除后保留的记录：TOMBSTONE_KEYS 中存在的键，status 为 PURGED，lifecycle 为原列表加 entry。
    其余字段（正文哈希、质检、时间段、重排信息、引擎 ID、原件哈希等）全部去掉。"""
    row = {key: record[key] for key in TOMBSTONE_KEYS if key in record}
    row["status"] = PURGED
    row["lifecycle"] = [*record.get("lifecycle", []), dict(entry)]
    return row


def blob_prefixes(record: Mapping[str, Any]) -> list[str]:
    """该版本在原件存储中的前缀：raw/<sha256>/（原件）与 converted/<sha256>/（转换结果）。
    记录没有合法的 sha256（如种子文档）→ []。"""
    digest = record.get("sha256")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        return []
    return [f"raw/{digest}/", f"converted/{digest}/"]


def orphan_entities(entities: Iterable[EntityRecord], mentions: Iterable[MentionRecord],
                    events: Iterable[EventRecord], mapping: Mapping[str, str]) -> set[str]:
    """无人提及的实体：不被任何原始提及或原始事件（主体、对象）引用，也不是被引用实体经合并（mapping）指向的实体。"""
    referenced = {mention.entity_id for mention in mentions}
    for event in events:
        referenced.update(id_ for id_ in (event.subject_entity, event.object_entity) if id_)
    referenced |= {mapping[id_] for id_ in referenced if id_ in mapping}
    return {entity.id for entity in entities if entity.id not in referenced}


def scrub_entity(entity: EntityRecord) -> EntityRecord:
    """抹去名称：canonical 为 ""，aliases 为空；ID、类型、候选不变（ID 不复用，合并决定仍然有效）。"""
    return replace(entity, canonical="", aliases=())


def scrubbed(entity: EntityRecord) -> bool:
    return entity.canonical == "" and not entity.aliases


def decision_related(decision: Decision, version_id: str, scrubbed_ids: Collection[str]) -> bool:
    """审核决定是否涉及被删文档或被抹去名称的实体：
    事件决定的事件属于该版本；冲突决定的两个事件之一属于该版本；引用决定由该版本提出、或指向该版本；
    实体决定的 target 或 value 在 scrubbed_ids 中。未收录问题的决定不涉及文档。
    批次 23c 补充（修正 23b 的遗漏）：关系否决的关系属于该版本（快照是该版本的引文）；子类决定的实体在 scrubbed_ids 中
    （说明中有实体名称）。系列决定不涉及正文：文档的文件名与标题按规格 37 保留在墓碑中，不抹去。"""
    prefix = version_id + ":"
    group = GROUPS[decision.kind]
    if group == "event":
        return decision.target.startswith(prefix)
    if group == "conflict":
        return any(part.startswith(prefix) for part in decision.target.split("|"))
    if group == "reference":
        return decision.target.startswith(version_id + "|") or decision.value == version_id
    if group == "entity":
        return decision.target in scrubbed_ids or decision.value in scrubbed_ids
    if group == "relation":
        return decision.target.startswith(prefix)
    if group == "subtype":
        return decision.target in scrubbed_ids
    return False


def scrub_decision(decision: Decision) -> Decision:
    """抹去引文快照与说明：snapshot 为 ()，label 为 SCRUBBED_LABEL；种类、对象、取值、编号、时间不变
    （事件与冲突决定的快照不再匹配任何事件，自然失效；实体合并决定仍然生效）。"""
    return replace(decision, snapshot=(), label=SCRUBBED_LABEL)


def turn_versions(turn: Mapping[str, Any]) -> set[str]:
    sources = list(turn.get("sources") or []) + [c["source"] for c in turn.get("citations") or []]
    return {source["version_id"] for source in sources if source.get("version_id")}


def scrub_turn(turn: Mapping[str, Any], version_ids: Collection[str]) -> dict[str, Any] | None:
    """对话中的一轮：资料或引文中有 version_ids 中的版本 → 返回抹去后的轮次（answer 为 PURGED_ANSWER，
    citations、sources、entities 清空，dropped_citations 为 0，"purged" 为真；问题、完整问题、时间等保留）；否则 None。"""
    if not turn_versions(turn) & set(version_ids):
        return None
    return {**turn, "answer": PURGED_ANSWER, "citations": [], "sources": [], "entities": [], "dropped_citations": 0,
            "purged": True}


def purge_entry(*, at: str, by: str) -> dict[str, Any]:
    return {"action": "purge", "at": at, "by": by, "reason": ""}

