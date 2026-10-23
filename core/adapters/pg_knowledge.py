"""知识数据的 PostgreSQL 实现（psycopg 3）。规格见 docs/spec-25-knowledge-db.md 与 core/app/knowledge.py。

- 表结构：core/adapters/pg_migrations/002_knowledge.sql（与登记处同一个 schema）。
- PgKnowledgeDb 持有一个连接和一把可重入锁；四个外观类共用它：
  PgKnowledgeStore（KnowledgeRepo）、PgLinkStore（LinkRepo）、PgDecisionStore（DecisionRepo）、PgKnowledgeQueries。
- 不在进程内缓存知识数据：每次调用都读数据库，多个进程可以同时读写。
- 审核决定的叠加结果物化在 entity_redirects / event_overrides 两张表，由纯函数 merge_map / effective_events 计算，
  在决定新增、撤销与抽取结果保存的同一事务中整体重算（决定条数很少，重算代价可以忽略）。
- 查询只取需要的行，再交给领域层纯函数渲染；结果必须与 FullScanQueries 完全相同（差分测试保证）。
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from threading import RLock
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from core.adapters.pg_registry import _connect
from core.app.extract import DocumentKnowledge
from core.app.link import DocumentReferences
from core.domain.entity_graph import (MAX_LIMIT, MIN_LIMIT, assemble_entity_graph, entity_node, event_edges,
                                      event_node)
from core.domain.entity_relations import RelationRecord, entity_relation_rows, relation_edges
from core.domain.events import Verdict
from core.domain.knowledge import (DETAIL_LIMIT, TIMELINE_LIMIT_MAX, EntityRecord, EventRecord, MentionRecord,
                                   build_timeline, entity_detail)
from core.domain.link_targets import RefTargets, TargetPlan, TargetState, apply_plan
from core.domain.relations import ConflictRecord, ReferenceRecord, action_key, build_conflicts
from core.domain.permissions import Visibility
from core.domain.purge import scrub_entity, scrubbed
from core.app.knowledge import CANDIDATE_COUNT_CAP
from core.domain.review import (Decision, DecisionKind, ReviewContext, decision_number, effective_entities,
                                effective_events, effective_relations, merge_map)

_VERSION = re.compile(r"v\d+_[0-9a-fA-F]{10}")
_DECISION_LOCK = 0x4841494E44  # "HAIND"
_TARGET_LOCK = 0x4841494E4C  # "HAINL"（批次 18b：引用目标同步）

# 可见事件（已发布文档、未被拒绝、非重复，review 视参数而定）。%(review)s 为布尔参数。
_VISIBLE = """
    SELECT ee.event_id, ee.version_id, ee.subject_entity AS s, ee.object_entity AS o,
           ee.raw_subject, ee.raw_object
    FROM effective_events ee
    JOIN documents d ON d.version_id = ee.version_id AND d.status = 'published'
    WHERE ee.verdict <> 'reject' AND NOT ee.duplicate AND (%(review)s OR ee.verdict <> 'review')
"""


_COPY_EVENTS = ("COPY events (event_id, version_id, ordinal, verdict, duplicate, subject_entity, object_entity, "
                "date_start, date_end, action_key, fingerprint, data, eff_verdict, eff_subject, eff_object) FROM STDIN")


def _event_row(event: EventRecord, version: str, ordinal: int, data: dict[str, Any]) -> tuple[Any, ...]:
    """events 表的一行；有效列先等于原始值，随后由 refresh_overlay 叠加。"""
    return (event.event_id, version, ordinal, event.verdict.value, bool(event.duplicate_of),
            event.subject_entity, event.object_entity, event.date_start, event.date_end, action_key(event.action),
            event.fingerprint or None, Jsonb(data), event.verdict.value, event.subject_entity, event.object_entity)


def _visible_of(column: str) -> str:
    """有效参与者（column 为 eff_subject 或 eff_object）在 %(ids)s 中的可见事件；只扫 events_subject/events_object 索引。"""
    return (f"SELECT e.event_id FROM events e "
            f"JOIN documents d ON d.version_id = e.version_id AND d.status = 'published' "
            f"WHERE e.{column} = ANY(%(ids)s) AND NOT e.duplicate AND e.eff_verdict <> 'reject' "
            f"AND (%(review)s OR e.eff_verdict <> 'review')")


# 与 %(ids)s 中任一实体相连的可见事件 ID（去重）。
_VISIBLE_OF_ENTITIES = f"({_visible_of('eff_subject')}) UNION ({_visible_of('eff_object')})"


class PgKnowledgeDb:
    def __init__(self, dsn: str, *, schema: str = "public") -> None:
        self._dsn, self._schema = dsn, schema
        self._lock = RLock()
        self._connection = _connect(dsn, schema)
        try:
            with self._connection.transaction():
                self._connection.execute("SELECT 1 FROM entities LIMIT 0")
                self._connection.execute("SELECT 1 FROM event_overrides LIMIT 0")
        except psycopg.errors.UndefinedTable as exc:
            self._connection.close()
            raise ValueError("知识数据尚未迁移，请先运行迁移工具") from exc
        except Exception:
            self._connection.close()
            raise

    @property
    def _conn(self) -> psycopg.Connection:
        if self._connection.closed or self._connection.broken:
            self._connection = _connect(self._dsn, self._schema)
        return self._connection

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def tx(self) -> Iterator[psycopg.Connection]:
        """加锁并开启事务（嵌套调用为保存点）。"""
        with self._lock:
            conn = self._conn
            with conn.transaction():
                yield conn

    @contextmanager
    def _read(self) -> Iterator[psycopg.Connection]:
        """独立查询使用一致只读快照；嵌套在写事务内时沿用其锁后视图。"""
        with self._lock:
            conn = self._conn
            independent = conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
            with conn.transaction():
                if independent:
                    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                yield conn

    # —— 读取原始记录 ——

    @staticmethod
    def decisions(conn: psycopg.Connection) -> list[Decision]:
        return [Decision.from_json(row[0]) for row in conn.execute("SELECT data FROM decisions ORDER BY number")]

    @staticmethod
    def entities(conn: psycopg.Connection, ids: Iterable[str]) -> list[EntityRecord]:
        """按创建顺序返回 ids 中存在的原始实体。"""
        return [EntityRecord.from_json(row[0]) for row in conn.execute("SELECT data FROM entities WHERE id=ANY(%s) ORDER BY ordinal", (list(ids),))]

    @staticmethod
    def raw_events(conn: psycopg.Connection, ids: Iterable[str]) -> list[EventRecord]:
        return [EventRecord.from_json(row[0]) for row in conn.execute("SELECT data FROM events WHERE event_id=ANY(%s) ORDER BY event_id", (list(ids),))]

    @staticmethod
    def redirects(conn: psycopg.Connection) -> dict[str, str]:
        return dict(conn.execute("SELECT source,target FROM entity_redirects"))

    @staticmethod
    def sources_of(conn: psycopg.Connection, ids: Iterable[str]) -> dict[str, list[str]]:
        """{最终实体: [合并到它的原始实体…]}（只含有来源的）。"""
        result: dict[str, list[str]] = {}
        for source, target in conn.execute("SELECT source,target FROM entity_redirects WHERE target=ANY(%s) ORDER BY source", (list(ids),)):
            result.setdefault(target, []).append(source)
        return result

    @staticmethod
    def decision_entity_ids(decisions: Iterable[Decision]) -> set[str]:
        return {id_ for decision in decisions if decision.group == "entity"
                for id_ in (decision.target, decision.value) if id_ is not None}

    def effective_entities(self, conn: psycopg.Connection, ids: Iterable[str]) -> dict[str, EntityRecord]:
        """ids 中属于有效实体的那些的有效记录（{id: EntityRecord}）。

        只取计算所需的闭包：ids、合并到它们的实体、它们的候选、全部合并决定涉及的实体；
        effective_entities 在这个子集上的结果与在全部实体上相同（见规格第三节）。"""
        wanted = set(ids)
        decisions = self.decisions(conn)
        needed = wanted | self.decision_entity_ids(decisions)
        needed.update(source for sources in self.sources_of(conn, wanted).values() for source in sources)
        records = self.entities(conn, needed)
        candidates = {id_ for record in records if record.id in wanted for id_ in record.review_candidates}
        records = self.entities(conn, needed | candidates)
        return {record.id: record for record in effective_entities(records, decisions) if record.id in wanted}

    @staticmethod
    def effective_rows(rows: Iterable[tuple[Any, str, str | None, str | None]]) -> list[EventRecord]:
        """(data, 有效 verdict, 有效主体, 有效对象) → 有效 EventRecord（与 effective_events 的结果相同）。"""
        return [replace(EventRecord.from_json(data), verdict=Verdict(verdict), subject_entity=subject, object_entity=object_)
                for data, verdict, subject, object_ in rows]

    @staticmethod
    def titles(conn: psycopg.Connection, versions: Iterable[str]) -> dict[str, str]:
        return dict(conn.execute("SELECT version_id,title FROM documents WHERE version_id=ANY(%s)", (list(versions),)))

    # —— 叠加结果 ——

    def refresh_overlay(self, conn: psycopg.Connection) -> None:
        """在当前事务中重算 entity_redirects 与 event_overrides。"""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DECISION_LOCK,))
        decisions = self.decisions(conn)
        mapping = merge_map(self.entities(conn, self.decision_entity_ids(decisions)), decisions)
        event_ids = [decision.target for decision in decisions if decision.group == "event"]
        raw = self.raw_events(conn, event_ids)
        overrides = {after.event_id: after.verdict.value
                     for before, after in zip(raw, effective_events(raw, decisions, mapping))
                     if before.verdict is not after.verdict}
        old_sources = [row[0] for row in conn.execute("SELECT source FROM entity_redirects")]
        old_events = [row[0] for row in conn.execute("SELECT event_id FROM event_overrides")]
        if old_sources:
            conn.execute("UPDATE events SET eff_subject=subject_entity WHERE subject_entity=ANY(%s)", (old_sources,))
            conn.execute("UPDATE events SET eff_object=object_entity WHERE object_entity=ANY(%s)", (old_sources,))
        if old_events:
            conn.execute("UPDATE events SET eff_verdict=verdict WHERE event_id=ANY(%s)", (old_events,))
        conn.execute("DELETE FROM entity_redirects")
        conn.execute("DELETE FROM event_overrides")
        with conn.cursor().copy("COPY entity_redirects(source,target) FROM STDIN") as writer:
            for source, target in mapping.items():
                writer.write_row((source, target))
        with conn.cursor().copy("COPY event_overrides(event_id,verdict) FROM STDIN") as writer:
            for id_, verdict in overrides.items():
                writer.write_row((id_, verdict))
        if mapping:
            conn.execute("UPDATE events e SET eff_subject=r.target FROM entity_redirects r WHERE e.subject_entity=r.source")
            conn.execute("UPDATE events e SET eff_object=r.target FROM entity_redirects r WHERE e.object_entity=r.source")
        if overrides:
            conn.execute("UPDATE events e SET eff_verdict=o.verdict FROM event_overrides o WHERE e.event_id=o.event_id")


# ====================================================================== 存储
class PgKnowledgeStore:
    """KnowledgeRepo 的 PostgreSQL 实现。"""

    def __init__(self, db: PgKnowledgeDb) -> None:
        self._db = db

    def entities(self) -> list[EntityRecord]:
        with self._db.tx() as conn:
            return [EntityRecord.from_json(row[0]) for row in conn.execute("SELECT data FROM entities ORDER BY ordinal")]

    def extracted(self) -> set[str]:
        with self._db.tx() as conn:
            return {row[0] for row in conn.execute("SELECT version_id FROM knowledge_docs")}

    def save(self, knowledge: DocumentKnowledge, *, extracted_at: str) -> None:
        if _VERSION.fullmatch(knowledge.version_id) is None:
            raise ValueError("文档版本号格式无效")
        document = {"version_id": knowledge.version_id, "extracted_at": extracted_at, "report": knowledge.report.to_json(),
                    "events": [event.to_json() for event in knowledge.events],
                    "mentions": [mention.to_json() for mention in knowledge.mentions],
                    "relations": [relation.to_json() for relation in knowledge.relations]}
        with self._db.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DECISION_LOCK,))
            self.import_entities(list(knowledge.new_entities))
            conn.execute("DELETE FROM knowledge_docs WHERE version_id=%s", (knowledge.version_id,))
            self.import_document(document)
            self._db.refresh_overlay(conn)

    def save_with(self, build: Callable[[list[EntityRecord]], DocumentKnowledge], *,
                  extracted_at: str) -> DocumentKnowledge:
        """批次 16b-Q5：在持有事务级咨询锁（与 save 相同）的同一事务内读取实体、调用 build、保存，跨进程互斥。"""
        with self._db.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DECISION_LOCK,))
            entities = [EntityRecord.from_json(row[0]) for row in conn.execute("SELECT data FROM entities ORDER BY ordinal")]
            knowledge = build(entities)
            self.save(knowledge, extracted_at=extracted_at)
            return knowledge

    def import_entities(self, entities: list[EntityRecord]) -> None:
        """迁移专用：按给定顺序追加实体（ID 已存在 → ValueError，整体不写入）。"""
        ids = [entity.id for entity in entities]
        if len(set(ids)) != len(ids):
            raise ValueError("实体 ID 重复")
        with self._db.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DECISION_LOCK,))
            if conn.execute("SELECT 1 FROM entities WHERE id=ANY(%s) LIMIT 1", (ids,)).fetchone():
                raise ValueError("实体 ID 重复")
            with conn.cursor().copy("COPY entities(id,type,has_candidates,data) FROM STDIN") as writer:
                for entity in entities:
                    writer.write_row((entity.id, entity.type.value, bool(entity.review_candidates), Jsonb(entity.to_json())))

    def import_document(self, document: dict[str, Any]) -> None:
        """迁移专用：写入 document() 形式的一份抽取结果（版本已存在 → ValueError），不重算叠加表。"""
        version = document["version_id"]
        if _VERSION.fullmatch(version) is None:
            raise ValueError("文档版本号格式无效")
        with self._db.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DECISION_LOCK,))
            if conn.execute("SELECT 1 FROM knowledge_docs WHERE version_id=%s", (version,)).fetchone():
                raise ValueError("抽取结果版本已存在")
            conn.execute("INSERT INTO knowledge_docs(version_id,extracted_at,report) VALUES (%s,%s,%s)",
                         (version, document["extracted_at"], Jsonb(document["report"])))
            with conn.cursor().copy(_COPY_EVENTS) as writer:
                for ordinal, data in enumerate(document["events"]):
                    writer.write_row(_event_row(EventRecord.from_json(data), version, ordinal, data))
            with conn.cursor().copy("COPY mentions(version_id,ordinal,entity_id,data) FROM STDIN") as writer:
                for ordinal, data in enumerate(document["mentions"]):
                    writer.write_row((version, ordinal, data["entity_id"], Jsonb(data)))
            with conn.cursor().copy("COPY entity_relations(relation_id,version_id,ordinal,kind,source_entity,"
                                    "target_entity,data) FROM STDIN") as writer:          # 批次 23b
                for ordinal, data in enumerate(document.get("relations", [])):
                    writer.write_row((data["relation_id"], version, ordinal, data["kind"], data["source_entity"],
                                      data["target_entity"], Jsonb(data)))

    def purge_document(self, version_id: str, mapping: Mapping[str, str]) -> dict[str, Any]:
        """批次 20b：删除 knowledge_docs 行（事件、提及级联删除）；无人提及的实体在同一事务中抹去名称；重算叠加表。"""
        with self._db.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DECISION_LOCK,))
            events = conn.execute("SELECT count(*) FROM events WHERE version_id=%s", (version_id,)).fetchone()[0]
            mentions = conn.execute("SELECT count(*) FROM mentions WHERE version_id=%s", (version_id,)).fetchone()[0]
            relations = conn.execute("SELECT count(*) FROM entity_relations WHERE version_id=%s", (version_id,)).fetchone()[0]
            conn.execute("DELETE FROM knowledge_docs WHERE version_id=%s", (version_id,))
            referenced = {row[0] for row in conn.execute(
                "SELECT entity_id FROM mentions UNION SELECT subject_entity FROM events WHERE subject_entity IS NOT NULL "
                "UNION SELECT object_entity FROM events WHERE object_entity IS NOT NULL")}
            referenced |= {mapping[id_] for id_ in referenced if id_ in mapping}
            changed: list[str] = []
            for id_, data in conn.execute("SELECT id,data FROM entities WHERE NOT (id = ANY(%s)) ORDER BY id",
                                          (sorted(referenced),)).fetchall():
                entity = EntityRecord.from_json(data)
                if not scrubbed(entity):
                    conn.execute("UPDATE entities SET data=%s WHERE id=%s", (Jsonb(scrub_entity(entity).to_json()), id_))
                    changed.append(id_)
            self._db.refresh_overlay(conn)
            return {"events": events, "mentions": mentions, "relations": relations, "entities_scrubbed": changed}

    def events(self) -> list[EventRecord]:
        with self._db.tx() as conn:
            return [EventRecord.from_json(row[0]) for row in conn.execute("SELECT data FROM events ORDER BY event_id")]

    def relations(self) -> list[RelationRecord]:
        with self._db.tx() as conn:
            return [RelationRecord.from_json(row[0])
                    for row in conn.execute("SELECT data FROM entity_relations ORDER BY relation_id")]

    def mentions(self) -> list[MentionRecord]:
        with self._db.tx() as conn:
            return [MentionRecord.from_json(row[0]) for row in conn.execute("SELECT data FROM mentions ORDER BY version_id,ordinal")]

    def document(self, version_id: str) -> dict[str, Any] | None:
        with self._db._read() as conn:
            row = conn.execute("SELECT extracted_at,report FROM knowledge_docs WHERE version_id=%s", (version_id,)).fetchone()
            if row is None:
                return None
            return {"version_id": version_id, "extracted_at": row[0], "report": row[1],
                    "events": [value[0] for value in conn.execute("SELECT data FROM events WHERE version_id=%s ORDER BY ordinal", (version_id,))],
                    "mentions": [value[0] for value in conn.execute("SELECT data FROM mentions WHERE version_id=%s ORDER BY ordinal", (version_id,))],
                    "relations": [value[0] for value in conn.execute(
                        "SELECT data FROM entity_relations WHERE version_id=%s ORDER BY ordinal", (version_id,))]}


class PgLinkStore:
    """LinkRepo 的 PostgreSQL 实现。"""

    def __init__(self, db: PgKnowledgeDb) -> None:
        self._db = db

    def extracted(self) -> set[str]:
        with self._db.tx() as conn:
            return {row[0] for row in conn.execute("SELECT version_id FROM reference_docs")}

    def save_references(self, result: DocumentReferences, *, extracted_at: str) -> None:
        if _VERSION.fullmatch(result.version_id) is None:
            raise ValueError("文档版本号格式无效")
        with self._db.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DECISION_LOCK,))
            conn.execute("DELETE FROM reference_docs WHERE version_id=%s", (result.version_id,))
            self.import_document({"version_id": result.version_id, "extracted_at": extracted_at,
                                  "report": result.report.to_json(), "references": [row.to_json() for row in result.references]})

    def import_document(self, document: dict[str, Any]) -> None:
        """迁移专用：写入 document() 形式的一份引用提取结果（版本已存在 → ValueError）。"""
        version = document["version_id"]
        if _VERSION.fullmatch(version) is None:
            raise ValueError("文档版本号格式无效")
        with self._db.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DECISION_LOCK,))
            if conn.execute("SELECT 1 FROM reference_docs WHERE version_id=%s", (version,)).fetchone():
                raise ValueError("引用提取结果版本已存在")
            conn.execute("INSERT INTO reference_docs(version_id,extracted_at,report) VALUES (%s,%s,%s)",
                         (version, document["extracted_at"], Jsonb(document["report"])))
            with conn.cursor().copy("COPY doc_references(ref_id,version_id,ordinal,data) FROM STDIN") as writer:
                for ordinal, data in enumerate(document["references"]):
                    writer.write_row((data["ref_id"], version, ordinal, Jsonb(data)))

    def references(self) -> list[ReferenceRecord]:
        with self._db.tx() as conn:
            return [ReferenceRecord.from_json(row[0]) for row in conn.execute("SELECT data FROM doc_references ORDER BY ref_id")]

    def document(self, version_id: str) -> dict[str, Any] | None:
        with self._db._read() as conn:
            row = conn.execute("SELECT extracted_at,report FROM reference_docs WHERE version_id=%s", (version_id,)).fetchone()
            if row is None:
                return None
            return {"version_id": version_id, "extracted_at": row[0], "report": row[1],
                    "references": [value[0] for value in conn.execute("SELECT data FROM doc_references WHERE version_id=%s ORDER BY ordinal", (version_id,))]}

    def purge_document(self, version_id: str) -> dict[str, int]:
        with self._db.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DECISION_LOCK,))
            references = conn.execute("SELECT count(*) FROM doc_references WHERE version_id=%s",
                                      (version_id,)).fetchone()[0]
            conn.execute("DELETE FROM reference_docs WHERE version_id=%s", (version_id,))
            conflicts = conn.execute("DELETE FROM conflicts WHERE version_a=%s OR version_b=%s",
                                     (version_id, version_id)).rowcount
            return {"references": references, "conflicts": conflicts}

    def conflicts(self) -> list[ConflictRecord]:
        with self._db.tx() as conn:
            return [ConflictRecord.from_json(row[0]) for row in conn.execute("SELECT data FROM conflicts ORDER BY pair_id")]

    def judged(self) -> set[str]:
        with self._db.tx() as conn:
            return {row[0] for row in conn.execute("SELECT pair_id FROM conflicts")}

    def add_conflict(self, record: ConflictRecord) -> None:
        with self._db.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DECISION_LOCK,))
            conn.execute("INSERT INTO conflicts(pair_id,event_a,event_b,version_a,version_b,verdict,data) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(pair_id) DO UPDATE SET event_a=EXCLUDED.event_a,event_b=EXCLUDED.event_b,version_a=EXCLUDED.version_a,version_b=EXCLUDED.version_b,verdict=EXCLUDED.verdict,data=EXCLUDED.data",
                         (record.pair_id, record.event_a, record.event_b, record.version_a, record.version_b, record.verdict.value, Jsonb(record.to_json())))


    # 批次 18b：引用目标的物化。版本号在 link_target_meta；写入在咨询锁内的同一事务中完成。
    def link_targets(self) -> TargetState:
        with self._db.tx() as conn:
            return self._load_targets(conn)

    @staticmethod
    def _load_targets(conn: psycopg.Connection) -> TargetState:
        version = conn.execute("SELECT version FROM link_target_meta WHERE id=1").fetchone()[0]
        docs = dict(conn.execute("SELECT version_id,title_key FROM link_target_docs"))
        targets = {row[0]: RefTargets(row[0], row[1], row[2], tuple(row[3]), tuple(row[4])) for row in
                   conn.execute("SELECT ref_id,version_id,title_key,exact,fuzzy FROM link_targets")}
        return TargetState(docs, targets, version)

    def link_targets_version(self) -> int:
        with self._db.tx() as conn:
            return conn.execute("SELECT version FROM link_target_meta WHERE id=1").fetchone()[0]

    def sync_link_targets(self, plan: Callable[[TargetState], TargetPlan]) -> TargetState:
        with self._db.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_TARGET_LOCK,))
            state = self._load_targets(conn)
            change = plan(state)
            if change.empty:
                return state
            if change.docs_delete:
                conn.execute("DELETE FROM link_target_docs WHERE version_id = ANY(%s)", (sorted(change.docs_delete),))
            if change.docs_set:
                conn.execute("DELETE FROM link_target_docs WHERE version_id = ANY(%s)", (sorted(change.docs_set),))
                with conn.cursor().copy("COPY link_target_docs(version_id,title_key) FROM STDIN") as writer:
                    for version, key in sorted(change.docs_set.items()):
                        writer.write_row((version, key))
            gone = sorted(change.targets_delete | {row.ref_id for row in change.targets_set})
            if gone:
                conn.execute("DELETE FROM link_targets WHERE ref_id = ANY(%s)", (gone,))
            if change.targets_set:
                with conn.cursor().copy("COPY link_targets(ref_id,version_id,title_key,exact,fuzzy) FROM STDIN") as writer:
                    for row in change.targets_set:
                        writer.write_row((row.ref_id, row.version_id, row.key, list(row.exact), list(row.fuzzy)))
            conn.execute("UPDATE link_target_meta SET version = %s WHERE id=1", (state.version + 1,))
            return apply_plan(state, change)


class PgDecisionStore:
    """DecisionRepo 的 PostgreSQL 实现。"""

    def __init__(self, db: PgKnowledgeDb) -> None:
        self._db = db

    def decisions(self) -> list[Decision]:
        with self._db.tx() as conn:
            return self._db.decisions(conn)

    def create(self, factory: Callable[[int], Decision]) -> Decision:
        with self._db.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DECISION_LOCK,))
            number = self.next_number()
            decision = factory(number)
            if decision_number(decision.decision_id) != number:
                raise ValueError("决定编号必须与分配的序号一致")
            conn.execute("DELETE FROM decisions WHERE grp=%s AND target=%s", (decision.group, decision.target))
            conn.execute("INSERT INTO decisions(number,grp,target,kind,data) VALUES (%s,%s,%s,%s,%s)",
                         (number, decision.group, decision.target, decision.kind.value, Jsonb(decision.to_json())))
            conn.execute("UPDATE kb_meta SET value=%s WHERE key='next_decision'", (str(number + 1),))
            self._db.refresh_overlay(conn)
            return decision

    def import_decisions(self, decisions: list[Decision], next_number: int) -> None:
        """迁移专用：仅目标处于初始状态（没有决定且下一个编号为 1）时整体导入并重算叠加表。
        目标不是初始状态 → ValueError，不写入；撤销全部决定后仍不得降低用过的编号。"""
        with self._db.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DECISION_LOCK,))
            if self.next_number() != 1 or conn.execute("SELECT 1 FROM decisions LIMIT 1").fetchone():
                raise ValueError("决定目标不是初始状态，不得导入")
            with conn.cursor().copy("COPY decisions(number,grp,target,kind,data) FROM STDIN") as writer:
                for decision in decisions:
                    writer.write_row((decision_number(decision.decision_id), decision.group, decision.target,
                                      decision.kind.value, Jsonb(decision.to_json())))
            conn.execute("UPDATE kb_meta SET value=%s WHERE key='next_decision'", (str(next_number),))
            self._db.refresh_overlay(conn)

    def rewrite(self, decisions: Iterable[Decision]) -> None:
        with self._db.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DECISION_LOCK,))
            for decision in decisions:
                try:
                    number = decision_number(decision.decision_id)
                except ValueError as exc:
                    raise KeyError(decision.decision_id) from exc
                updated = conn.execute("UPDATE decisions SET data=%s WHERE number=%s AND grp=%s AND target=%s AND kind=%s",
                                       (Jsonb(decision.to_json()), number, decision.group, decision.target,
                                        decision.kind.value)).rowcount
                if updated != 1:
                    raise KeyError(decision.decision_id)
            self._db.refresh_overlay(conn)

    def next_number(self) -> int:
        with self._db.tx() as conn:
            return int(conn.execute("SELECT value FROM kb_meta WHERE key='next_decision'").fetchone()[0])

    def remove(self, decision_id: str) -> Decision | None:
        try:
            number = decision_number(decision_id)
        except ValueError:
            return None
        with self._db.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DECISION_LOCK,))
            row = conn.execute("DELETE FROM decisions WHERE number=%s AND data->>'decision_id'=%s RETURNING data", (number, decision_id)).fetchone()
            if row is None:
                return None
            self._db.refresh_overlay(conn)
            return Decision.from_json(row[0])


# ====================================================================== 查询
class PgKnowledgeQueries:
    """KnowledgeQueries 的 PostgreSQL 实现。"""

    def __init__(self, db: PgKnowledgeDb) -> None:
        self._db = db

    def merge_map(self) -> dict[str, str]:
        with self._db._read() as conn:
            return self._db.redirects(conn)

    # —— 公共 ——

    def _events_where(self, conn: psycopg.Connection, where: str, params: dict[str, Any], *,
                      order: str = "ee.event_id", limit: int | None = None) -> list[EventRecord]:
        sql = "SELECT ee.data,ee.verdict,ee.subject_entity,ee.object_entity FROM effective_events ee WHERE " + where
        sql += " ORDER BY " + order
        if limit is not None:
            sql += " LIMIT %(row_limit)s"
            params = {**params, "row_limit": limit}
        return self._db.effective_rows(conn.execute(sql, params))

    def _valid_entities(self, conn: psycopg.Connection, ids: Iterable[str]) -> set[str]:
        """ids 中属于有效实体的（存在且没有被合并）。"""
        return {row[0] for row in conn.execute("SELECT e.id FROM entities e WHERE e.id=ANY(%s) AND NOT EXISTS (SELECT 1 FROM entity_redirects r WHERE r.source=e.id)", (list(ids),))}

    def _scoped_relations(self, conn: psycopg.Connection, where: str, params: dict[str, Any],
                          scope: Visibility | None) -> list[RelationRecord]:
        """批次 23b：满足 where（作用于原始两端，别名 r）且文档可读的关系，叠加决定后的有效关系（effective_relations）。"""
        readable, scope_params = self._scope_sql(scope)
        rows = conn.execute("SELECT r.data FROM entity_relations r WHERE (" + where + ") AND r.version_id IN (" +
                            readable + ") ORDER BY r.relation_id", {**scope_params, **params})
        records = [RelationRecord.from_json(row[0]) for row in rows]
        if not records:
            return []
        return effective_relations(records, self._db.decisions(conn), self._db.redirects(conn))

    def _relation_edges(self, conn: psycopg.Connection, relations: list[RelationRecord],
                        scope: Visibility | None) -> list[dict[str, Any]]:
        """批次 23b-r2：有效关系聚合成的关系边，只保留两端都是范围内有效实体的（与 FullScan 的实体范围一致）。"""
        ends = {id_ for relation in relations for id_ in (relation.source_entity, relation.target_entity)}
        return relation_edges(relations, self._scoped_entities(conn, list(ends), scope)) if ends else []

    def _raw_ids(self, conn: psycopg.Connection, ids: Iterable[str]) -> list[str]:
        """有效实体 ID 加上合并到它们的原始 ID。"""
        ids = list(ids)
        return ids + [source for sources in self._db.sources_of(conn, ids).values() for source in sources]


    # —— 按文档授权的查询；不限范围的既有路径保持不变 ——

    @staticmethod
    def _scope_sql(scope: Visibility | None, *, inline: bool = False) -> tuple[str, dict[str, Any]]:
        """范围先于聚合和上限；时间轴内联半连接，其他查询可物化范围并复用。
        批次 20a：只含当前为已发布的文档；scope 为 None 时为全部已发布文档（所有公开查询都走带范围的路径）。"""
        if scope is None:
            return "SELECT version_id FROM documents WHERE status='published'", {}
        sql = """SELECT d.version_id FROM documents d JOIN folders f ON f.id=d.folder_id
            WHERE d.status='published' AND f.path=ANY(%(scope_folders)s) AND NOT (d.version_id=ANY(%(scope_deny)s))
            UNION SELECT version_id FROM documents WHERE status='published' AND version_id=ANY(%(scope_allow)s)"""
        if inline:
            # UNION 会阻断时间索引的保序半连接，导致先扫描宽事件行再排序。
            sql = """SELECT d.version_id FROM documents d JOIN folders f ON f.id=d.folder_id
                WHERE d.status='published' AND (d.version_id=ANY(%(scope_allow)s)
                    OR (f.path=ANY(%(scope_folders)s) AND NOT (d.version_id=ANY(%(scope_deny)s))))"""
        return sql, {"scope_folders": sorted(scope.folders), "scope_allow": sorted(scope.allow),
                     "scope_deny": sorted(scope.deny)}

    def _scoped_referenced(self, conn: psycopg.Connection, ids: Iterable[str],
                           scope: Visibility) -> set[str]:
        """在裁剪后的有效提及或任意有效事件中出现过的指定实体 ID。包含拒绝、重复及未发布事件。"""
        readable, params = self._scope_sql(scope)
        params["entity_ids"] = list(ids)
        # 这里只判定存在性；高频实体也无需枚举其全部提及和事件。
        return {row[0] for row in conn.execute("""WITH readable AS MATERIALIZED (""" + readable + """)
            SELECT id FROM unnest(%(entity_ids)s::text[]) wanted(id) WHERE EXISTS (
                SELECT 1 FROM mentions m WHERE m.entity_id=ANY(ARRAY(
                    SELECT wanted.id UNION SELECT source FROM entity_redirects WHERE target=wanted.id))
                    AND m.version_id IN (SELECT version_id FROM readable))
            OR EXISTS (SELECT 1 FROM events e WHERE e.eff_subject=wanted.id
                AND e.version_id IN (SELECT version_id FROM readable))
            OR EXISTS (SELECT 1 FROM events e WHERE e.eff_object=wanted.id
                AND e.version_id IN (SELECT version_id FROM readable))""", params)}

    def _scoped_entities(self, conn: psycopg.Connection, ids: Iterable[str],
                         scope: Visibility) -> dict[str, EntityRecord]:
        """先叠加全局决定，再用 SQL 求出的可见引用集合裁剪实体和候选；不在 Python 按版本过滤。"""
        entities = self._db.effective_entities(conn, ids)
        candidates = {id_ for entity in entities.values() for id_ in entity.review_candidates}
        referenced = self._scoped_referenced(conn, set(entities) | candidates, scope)
        return {id_: replace(entity, review_candidates=tuple(candidate for candidate in entity.review_candidates
                                                              if candidate in referenced))
                for id_, entity in entities.items() if id_ in referenced}

    def _scoped_detail(self, entity_id: str, limit: int, scope: Visibility) -> dict[str, Any] | None:
        readable, params = self._scope_sql(scope)
        predicate = "ee.version_id IN (" + readable + ")"
        with self._db._read() as conn:
            row = conn.execute("SELECT target FROM entity_redirects WHERE source=%s", (entity_id,)).fetchone()
            final = row[0] if row else entity_id
            entities = self._scoped_entities(conn, [final], scope)
            if final not in entities:
                return None
            entity = entities[final]
            if entity.review_candidates:
                entities.update(self._scoped_entities(conn, entity.review_candidates, scope))
            values = {**params, "raw_ids": self._raw_ids(conn, [final]), "entity": final}
            count_sql = """SELECT m.version_id,count(*),sum(count(*)) OVER () FROM mentions m
                WHERE m.entity_id=ANY(%(raw_ids)s) AND m.version_id IN (""" + readable + """)
                GROUP BY m.version_id ORDER BY m.version_id"""
            if limit > 0:
                count_sql += " LIMIT %(limit)s"
                values["limit"] = limit + 1
            counts, total = {}, 0
            for version, count, mention_total in conn.execute(count_sql, values):
                counts[version], total = count, int(mention_total)
            visible = predicate + """ AND (ee.subject_entity=%(entity)s OR ee.object_entity=%(entity)s)
                AND ee.verdict<>'reject' AND NOT ee.duplicate"""
            if limit > 0:
                events = self._scoped_entity_events(conn, final, True, limit + 1, scope)
                events += self._scoped_entity_events(conn, final, False, limit + 1, scope)
            else:
                events = self._events_where(conn, visible, values)
            relations = self._scoped_relations(conn, "r.source_entity=ANY(%(raw_ids)s) OR r.target_entity=ANY(%(raw_ids)s)",
                                               {"raw_ids": values["raw_ids"]}, scope)
            others = {id_ for r in relations for id_ in (r.source_entity, r.target_entity)} - set(entities)
            known = {**entities, **(self._scoped_entities(conn, others, scope) if others else {})}
            titles = self._db.titles(conn, set(counts) | {event.version_id for event in events}
                                     | {r.version_id for r in relations})
            data = entity_detail(entity, entities, counts, events, titles, limit=limit, mention_total=total,
                                 redirected_from=entity_id if entity_id != final else None)
            data["relations"], data["relations_truncated"] = entity_relation_rows(final, relations, known, titles,
                                                                                  limit=limit)
            return data

    def _scoped_entity_events(self, conn: psycopg.Connection, entity_id: str, dated: bool,
                              limit: int, scope: Visibility) -> list[EventRecord]:
        """两个参与者分别按索引取可读事件，再合并；范围过滤先于各自的排序和上限。"""
        readable, params = self._scope_sql(scope)
        params.update(entity=entity_id, limit=limit)
        order = "date_start DESC,date_end DESC,event_id DESC" if dated else "event_id"
        condition = "date_start IS NOT NULL" if dated else "date_start IS NULL"
        parts = ["(SELECT event_id,date_start,date_end FROM events WHERE " + column +
                 "=%(entity)s AND NOT duplicate AND eff_verdict<>'reject' AND " + condition +
                 " AND (version_id IN (SELECT version_id FROM readable)) IS TRUE ORDER BY " + order + " LIMIT %(limit)s)"
                 for column in ("eff_subject", "eff_object")]
        sql = ("WITH readable AS MATERIALIZED (" + readable + ") SELECT event_id FROM (" + " UNION ".join(parts) +
               ") selected ORDER BY " + order + " LIMIT %(limit)s")
        ids = [row[0] for row in conn.execute(sql, params)]
        return self._events_where(conn, "ee.event_id=ANY(%(ids)s)", {"ids": ids})

    def _scoped_limited_events(self, conn: psycopg.Connection, where: str, params: dict[str, Any], *,
                                limit: int, order: str = "ee.event_id") -> list[EventRecord]:
        """先在范围内按索引排序取 ID，再读取有限条正文，避免宽 JSON 参与扫描和排序。"""
        sql = "SELECT ee.event_id FROM effective_events ee WHERE " + where
        ids = [row[0] for row in conn.execute(sql + " ORDER BY " + order + " LIMIT %(row_limit)s",
                                             {**params, "row_limit": limit})]
        events = {event.event_id: event for event in
                  self._events_where(conn, "ee.event_id=ANY(%(ids)s)", {"ids": ids})}
        return [events[id_] for id_ in ids]

    def _scoped_timeline(self, review: bool, start: date | None, end: date | None, limit: int,
                         scope: Visibility) -> dict[str, Any]:
        if not 1 <= limit <= TIMELINE_LIMIT_MAX:
            raise ValueError("时间轴条数上限超出范围")
        if start is not None and end is not None and start > end:
            raise ValueError("时间范围起点晚于终点")
        readable, params = self._scope_sql(scope, inline=True)
        params.update(review=review, start=start, end=end)
        # 布尔子计划保留事件索引顺序，避免小范围时反复扫描可读版本的嵌套循环。
        visible = "(ee.version_id IN (" + readable + """)) IS TRUE AND ee.verdict<>'reject' AND NOT ee.duplicate
            AND (%(review)s OR ee.verdict<>'review')"""
        dated_where = visible + " AND ee.date_start IS NOT NULL"
        if start is not None:
            dated_where += " AND COALESCE(ee.date_end,ee.date_start)>=%(start)s"
        if end is not None:
            dated_where += " AND ee.date_start<=%(end)s"
        with self._db._read() as conn:
            dated = self._scoped_limited_events(conn, dated_where, params,
                order="ee.date_start DESC,ee.date_end DESC,ee.event_id DESC", limit=limit + 1)
            undated = (self._scoped_limited_events(conn, visible + " AND ee.date_start IS NULL", params, limit=limit + 1)
                       if start is None and end is None else [])
            selected = dated[:limit] + undated[:limit]
            result = build_timeline(selected, self._db.titles(conn, {event.version_id for event in selected}),
                                    include_review=review)
            return {**result, "truncated": len(dated) > limit or len(undated) > limit}

    def _scoped_counts(self, scope: Visibility) -> dict[str, Any]:
        readable, params = self._scope_sql(scope)
        prefix = "WITH readable AS MATERIALIZED (" + readable + ") "
        inline, _ = self._scope_sql(scope, inline=True)
        with self._db._read() as conn:
            # 此处只有一次范围连接，内联可让 PostgreSQL 并行聚合事件。
            counts = conn.execute("WITH readable AS NOT MATERIALIZED (" + inline + ") " +
                """SELECT count(*) FILTER (WHERE eff_verdict='pass' AND NOT duplicate),
                count(*) FILTER (WHERE eff_verdict='review' AND NOT duplicate),
                count(*) FILTER (WHERE eff_verdict='reject'),count(*) FILTER (WHERE duplicate)
                FROM events e JOIN readable v ON v.version_id=e.version_id""", params).fetchone()
            # 先找直接提及，缺失时才查合并来源和事件；不枚举数百万重复引用。
            # source 是非空主键，NOT IN 可使用哈希子计划而无需逐实体反连接。
            entities = conn.execute(prefix + """SELECT count(*) FROM entities wanted
                WHERE wanted.id NOT IN (SELECT source FROM entity_redirects) AND (
                EXISTS (SELECT 1 FROM mentions m WHERE m.entity_id=wanted.id
                    AND m.version_id IN (SELECT version_id FROM readable))
                OR EXISTS (SELECT 1 FROM entity_redirects r JOIN mentions m ON m.entity_id=r.source
                    WHERE r.target=wanted.id AND m.version_id IN (SELECT version_id FROM readable))
                OR EXISTS (SELECT 1 FROM events e WHERE e.eff_subject=wanted.id
                    AND e.version_id IN (SELECT version_id FROM readable))
                OR EXISTS (SELECT 1 FROM events e WHERE e.eff_object=wanted.id
                    AND e.version_id IN (SELECT version_id FROM readable)))""", params).fetchone()[0]
            extracted = conn.execute(prefix + """SELECT count(*) FROM knowledge_docs k
                JOIN readable v ON v.version_id=k.version_id""", params).fetchone()[0]
            return {"extracted": extracted, "entities": entities,
                    "events": dict(zip(("pass", "review", "reject", "duplicates"), counts))}

    def _scoped_review_entities(self, limit: int, scope: Visibility) -> list[dict[str, Any]]:
        if limit == 0:
            return []
        with self._db._read() as conn:
            result: list[dict[str, Any]] = []
            after: str | None = None
            while limit < 0 or len(result) < limit:
                rows = conn.execute("""SELECT e.id FROM entities e WHERE has_candidates
                    AND NOT EXISTS (SELECT 1 FROM entity_redirects r WHERE r.source=e.id)
                    AND (%s::text IS NULL OR e.id>%s) ORDER BY e.id LIMIT 200""", (after, after)).fetchall()
                if not rows:
                    break
                ids = [row[0] for row in rows]
                entities = self._scoped_entities(conn, ids, scope)
                candidates = self._db.effective_entities(conn,
                    {id_ for entity in entities.values() for id_ in entity.review_candidates})
                for id_ in ids:
                    entity = entities.get(id_)
                    if entity is not None and entity.review_candidates:
                        result.append({"id": id_, "type": entity.type.value, "subtype": entity.subtype,
                                       "canonical": entity.canonical,
                            "candidates": [{"id": key, "canonical": candidates[key].canonical}
                                           for key in entity.review_candidates if key in candidates]})
                after = ids[-1]
            return result[:limit]

    def _scoped_review_events(self, limit: int, scope: Visibility) -> list[dict[str, Any]]:
        readable, params = self._scope_sql(scope)
        where = "(ee.version_id IN (" + readable + ")) IS TRUE AND ee.verdict='review' AND NOT ee.duplicate"
        with self._db._read() as conn:
            if limit < 0:
                count = conn.execute("SELECT count(*) FROM effective_events ee WHERE " + where, params).fetchone()[0]
                row_limit = max(0, count + limit)
            else:
                row_limit = limit
            events = self._scoped_limited_events(conn, where, params, limit=row_limit)
            timeline = build_timeline(events, self._db.titles(conn, {event.version_id for event in events}))
            return sorted(timeline["items"] + timeline["undated"], key=lambda row: row["id"])

    def _scoped_conflicts(self, pair_id: str | None, scope: Visibility) -> list[dict[str, Any]]:
        readable, params = self._scope_sql(scope)
        sql = """WITH readable AS MATERIALIZED (""" + readable + """)
            SELECT c.data FROM conflicts c
            JOIN documents a ON a.version_id=c.version_a AND a.status='published'
            JOIN documents b ON b.version_id=c.version_b AND b.status='published'
            WHERE c.verdict IN ('conflict','review')
                AND c.version_a IN (SELECT version_id FROM readable)
                AND c.version_b IN (SELECT version_id FROM readable)"""
        if pair_id is not None:
            sql += " AND c.pair_id=%(pair)s"
            params["pair"] = pair_id
        with self._db._read() as conn:
            records = [ConflictRecord.from_json(row[0]) for row in conn.execute(sql + " ORDER BY c.pair_id", params)]
            events = {event.event_id: event for event in self._events_where(conn,
                "ee.event_id=ANY(%(ids)s) AND ee.verdict<>'reject' AND ee.version_id IN (" + readable + ")",
                {**params, "ids": list({id_ for record in records for id_ in (record.event_a, record.event_b)})})}
            versions = {v for record in records for v in (record.version_a, record.version_b)}
            titles = self._db.titles(conn, versions | {event.version_id for event in events.values()})
            return build_conflicts(records, events, titles, versions)

    def _scoped_graph(self, review: bool, focus: str | None, depth: int, limit: int,
                      scope: Visibility) -> dict[str, Any]:
        if not MIN_LIMIT <= limit <= MAX_LIMIT:
            raise ValueError("节点上限超出范围")
        if focus is not None and not 1 <= depth <= 3:
            raise ValueError("局部图深度超出范围")
        readable, params = self._scope_sql(scope)
        params.update(review=review, limit=limit)
        # 只读取构图所需的标识与边；先按版本裁剪，再求度数或展开邻接层。
        linked = """WITH readable AS MATERIALIZED (""" + readable + """), linked AS MATERIALIZED (
            SELECT e.event_id,e.version_id,s.id AS s,o.id AS o FROM events e
            JOIN readable v ON v.version_id=e.version_id
            JOIN documents d ON d.version_id=e.version_id AND d.status='published'
            LEFT JOIN entities s ON s.id=e.eff_subject LEFT JOIN entities o ON o.id=e.eff_object
            WHERE NOT e.duplicate AND e.eff_verdict<>'reject' AND (%(review)s OR e.eff_verdict<>'review')
                AND (s.id IS NOT NULL OR o.id IS NOT NULL)) """
        with self._db._read() as conn:
            if focus is None:
                # 批次 23b-r2：关系边（聚合后）在 Python 中算出，按端点度数并入 SQL 的排序，上限仍在 SQL 中执行。
                relation_degree: Counter[str] = Counter(
                    edge[side] for edge in self._relation_edges(conn, self._scoped_relations(conn, "TRUE", {}, scope), scope)
                    for side in ("source", "target"))
                params.update(rel_nodes=list(relation_degree), rel_degrees=list(relation_degree.values()))
                rows = conn.execute(linked + """, degrees AS (
                    SELECT node,sum(degree) AS degree FROM (
                        SELECT ('ent:'||id) COLLATE "C" AS node,1 AS degree FROM (
                            SELECT s AS id FROM linked WHERE s IS NOT NULL
                            UNION ALL SELECT o FROM linked WHERE o IS NOT NULL) sides
                        UNION ALL SELECT u.node COLLATE "C",u.degree
                            FROM unnest(%(rel_nodes)s::text[],%(rel_degrees)s::int[]) AS u(node,degree)) parts
                    GROUP BY node)
                    SELECT node,degree,count(*) OVER ()+(SELECT count(*) FROM linked)
                    FROM degrees ORDER BY degree DESC,node LIMIT %(limit)s""", params).fetchall()
                total = rows[0][2] if rows else 0
                candidates = [(row[0], row[1]) for row in rows]
                if rows and (len(rows) < limit or rows[-1][1] < 2):
                    candidates.extend(conn.execute(linked + """SELECT ('evt:'||event_id) COLLATE "C" AS node,
                        (s IS NOT NULL)::int+(o IS NOT NULL)::int AS degree FROM linked
                        ORDER BY degree DESC,node LIMIT %(limit)s""", params))
                kept = [node for node, _ in sorted(candidates, key=lambda row: (-row[1], row[0]))[:limit]]
                complete = True
            else:
                # 局部查询通过参与者索引定位，避免为一个节点物化整个可读图。
                def neighbors(layer: list[str], seen: set[str], room: int) -> tuple[int, list[str]]:
                    # 批次 23b-r2：一层中可以同时有实体与事件；实体另经关系边连到实体。
                    entity_layer = [node[4:] for node in layer if node.startswith("ent:")]
                    event_layer = [node[4:] for node in layer if node.startswith("evt:")]
                    related: set[str] = set()
                    in_layer = set(entity_layer)
                    if entity_layer:
                        raw = self._raw_ids(conn, entity_layer)
                        for edge in self._relation_edges(conn, self._scoped_relations(
                                conn, "r.source_entity=ANY(%(raw)s) OR r.target_entity=ANY(%(raw)s)", {"raw": raw}, scope), scope):
                            if edge["source"][4:] in in_layer:
                                related.add(edge["target"])
                            if edge["target"][4:] in in_layer:
                                related.add(edge["source"])
                    values = {**params, "entity_ids": entity_layer, "event_ids": event_layer, "related": sorted(related),
                              "seen": list(seen), "room": room}
                    visible = """e.version_id IN (""" + readable + """) AND NOT e.duplicate
                        AND e.eff_verdict<>'reject' AND (%(review)s OR e.eff_verdict<>'review')
                        AND EXISTS (SELECT 1 FROM documents d WHERE d.version_id=e.version_id AND d.status='published')"""
                    parts = ['SELECT unnest(%(related)s::text[]) COLLATE "C" AS node']
                    if entity_layer:
                        parts += ["""SELECT ('evt:'||e.event_id) COLLATE "C" AS node FROM events e
                                WHERE e.""" + col + "=ANY(%(entity_ids)s) AND " + visible
                                  for col in ("eff_subject", "eff_object")]
                    if event_layer:
                        parts += ["""SELECT ('ent:'||x.id) COLLATE "C" AS node FROM events e
                                JOIN entities x ON x.id=e.""" + col +
                                  " WHERE e.event_id=ANY(%(event_ids)s) AND " + visible
                                  for col in ("eff_subject", "eff_object")]
                    sql = " UNION ".join(parts)
                    rows = conn.execute("SELECT node,count(*) OVER () FROM (" + sql +
                        ') candidates WHERE NOT (node=ANY(%(seen)s)) ORDER BY node COLLATE "C" LIMIT %(room)s',
                        values).fetchall()
                    return (rows[0][1] if rows else 0), [row[0] for row in rows]
                if not focus.startswith(("ent:", "evt:")):
                    raise ValueError("未找到图谱节点")
                if focus.startswith("ent:") and not self._valid_entities(conn, [focus[4:]]):
                    raise ValueError("未找到图谱节点")
                kept, layer, seen = [focus], [focus], {focus}
                total, complete = 1, True
                for level in range(1, depth + 1):
                    count, next_nodes = neighbors(layer, seen, limit - len(kept))
                    if not count:
                        if level == 1:
                            raise ValueError("未找到图谱节点")
                        break
                    total += count
                    kept.extend(next_nodes)
                    if total >= limit:
                        complete = level == depth
                        break
                    seen.update(next_nodes)
                    layer = next_nodes
            entity_ids = [node[4:] for node in kept if node.startswith("ent:")]
            event_ids = [node[4:] for node in kept if node.startswith("evt:")]
            entities = self._scoped_entities(conn, entity_ids, scope)
            events = self._events_where(conn,
                "ee.event_id=ANY(%(ids)s) AND ee.version_id IN (" + readable + ")", {**params, "ids": event_ids})
            titles = self._db.titles(conn, {event.version_id for event in events})
            counts: dict[str, tuple[int, int]] = {}
            for id_, count, documents in conn.execute("""SELECT COALESCE(r.target,m.entity_id),count(*),
                count(DISTINCT m.version_id) FROM mentions m
                JOIN documents d ON d.version_id=m.version_id AND d.status='published'
                LEFT JOIN entity_redirects r ON r.source=m.entity_id
                WHERE m.entity_id=ANY(%(raw_ids)s) AND m.version_id IN (""" + readable + """)
                GROUP BY COALESCE(r.target,m.entity_id)""",
                {**params, "raw_ids": self._raw_ids(conn, entity_ids)}):
                counts[id_] = (count, documents)
            nodes = {"ent:" + id_: entity_node(entity, *counts.get(id_, (0, 0)))
                     for id_, entity in entities.items()}
            edges: list[dict[str, Any]] = []
            for event in events:
                nodes["evt:" + event.event_id] = event_node(event, titles.get(event.version_id, event.version_id))
                edges.extend(event_edges(event, entities))
            if entities:                                                # 批次 23b-r2：保留实体之间的关系边
                raw = self._raw_ids(conn, list(entities))
                edges.extend(relation_edges(self._scoped_relations(
                    conn, "r.source_entity=ANY(%(raw)s) AND r.target_entity=ANY(%(raw)s)", {"raw": raw}, scope), entities))
            return assemble_entity_graph(nodes, edges, kept, total_nodes=total, truncated=total > limit,
                                         total_complete=complete)


    # —— 实体详情 ——

    def entity_detail(self, entity_id: str, *, limit: int = DETAIL_LIMIT,
                      scope: Visibility | None = None) -> dict[str, Any] | None:
        return self._scoped_detail(entity_id, limit, scope)  # 批次 20a：不限范围也走带范围的路径（全部已发布文档）

    def _entity_events(self, conn: psycopg.Connection, entity_id: str, *, dated: bool, limit: int) -> list[EventRecord]:
        """以 entity_id 为有效主体或有效对象、未被拒绝、非重复的事件：dated 为真时按 (开始, 结束, ID) 倒序取前 limit 条，
        否则取无日期事件按 ID 升序前 limit 条。两个参与者分别走各自的索引再合并。"""
        order = "date_start DESC,date_end DESC,event_id DESC" if dated else "event_id"
        condition = "date_start IS NOT NULL" if dated else "date_start IS NULL"
        parts = []
        for column in ("eff_subject", "eff_object"):
            parts.append("(SELECT event_id,date_start,date_end FROM events WHERE " + column +
                         "=%(id)s AND NOT duplicate AND eff_verdict<>'reject' AND " + condition +
                         " ORDER BY " + order + " LIMIT %(limit)s)")
        sql = "SELECT event_id FROM (" + " UNION ".join(parts) + ") selected ORDER BY " + order + " LIMIT %(limit)s"
        ids = [row[0] for row in conn.execute(sql, {"id": entity_id, "limit": limit})]
        return self._events_where(conn, "ee.event_id=ANY(%(ids)s)", {"ids": ids})

    # —— 实体关系图 ——

    def entity_graph(self, *, include_review: bool = True, focus: str | None = None, depth: int = 1,
                     limit: int = 300, scope: Visibility | None = None) -> dict[str, Any]:
        return self._scoped_graph(include_review, focus, depth, limit, scope)  # 批次 20a：不限范围也走带范围的路径

    def _global_nodes(self, conn: psycopg.Connection, params: dict[str, Any], limit: int) -> tuple[list[str], int]:
        """索引覆盖的窄行聚合；事件度数至多二，先选实体，必要时才补事件候选。"""
        branches = []
        for condition in ("eff_subject IS NOT NULL", "eff_subject IS NULL AND eff_object IS NOT NULL"):
            branches.append("SELECT event_id,version_id,eff_subject AS s,eff_object AS o FROM events "
                            "WHERE NOT duplicate AND eff_verdict<>'reject' AND " + condition +
                            " AND (%(review)s OR eff_verdict<>'review')")
        linked = """WITH linked AS MATERIALIZED (
            SELECT e.event_id,s.id AS s,o.id AS o FROM (""" + " UNION ALL ".join(branches) + """ ) e
            JOIN documents d ON d.version_id=e.version_id AND d.status='published'
            LEFT JOIN entities s ON s.id=e.s LEFT JOIN entities o ON o.id=e.o
            WHERE s.id IS NOT NULL OR o.id IS NOT NULL) """
        values = {**params, "limit": limit}
        rows = conn.execute(linked + """, degrees AS (
            SELECT ('ent:'||id) COLLATE "C" AS node,count(*) AS degree FROM (
                SELECT s AS id FROM linked WHERE s IS NOT NULL
                UNION ALL SELECT o FROM linked WHERE o IS NOT NULL) sides GROUP BY id)
            SELECT node,degree,count(*) OVER ()+(SELECT count(*) FROM linked)
            FROM degrees ORDER BY degree DESC,node LIMIT %(limit)s""", values).fetchall()
        if not rows:
            return [], 0
        candidates = [(row[0], row[1]) for row in rows]
        # 相同度数下 ent: 排在 evt: 前；已有 limit 个度数至少二的实体时，事件不可能入选。
        if len(rows) < limit or rows[-1][1] < 2:
            candidates.extend(conn.execute(linked + """SELECT ('evt:'||event_id) COLLATE "C" AS node,
                (s IS NOT NULL)::int+(o IS NOT NULL)::int AS degree FROM linked
                ORDER BY degree DESC,node LIMIT %(limit)s""", values))
        kept = [node for node, _ in sorted(candidates, key=lambda row: (-row[1], row[0]))[:limit]]
        return kept, rows[0][2]

    def _exists(self, conn: psycopg.Connection, node: str, params: dict[str, Any]) -> bool:
        if node.startswith("ent:"):
            id_ = node[4:]
            if not self._valid_entities(conn, [id_]):
                return False
            # 存在性不需要 UNION 去重；两个方向各自找到一条即可停止。
            for column in ("eff_subject", "eff_object"):
                if conn.execute(_visible_of(column) + " LIMIT 1", {**params, "ids": [id_]}).fetchone():
                    return True
            return False
        if node.startswith("evt:"):
            return conn.execute("SELECT 1 FROM (" + _VISIBLE + ") v WHERE v.event_id=%(id)s AND (EXISTS (SELECT 1 FROM entities WHERE id=v.s) OR EXISTS (SELECT 1 FROM entities WHERE id=v.o))",
                                {**params, "id": node[4:]}).fetchone() is not None
        return False

    def _next_layer(self, conn: psycopg.Connection, layer: list[str], seen: set[str], params: dict[str, Any], *,
                    room: int) -> tuple[int, list[str]]:
        """(下一层节点数, 下一层节点 ID 升序)。节点数 ≥ room 时只返回前 room 个 ID（够用即可）。"""
        sql, values = self._neighbor_query(layer, params)
        rows = conn.execute("SELECT node,count(*) OVER () FROM (" + sql +
                            ') neighbors WHERE NOT (node=ANY(%(seen)s)) ORDER BY node COLLATE "C" LIMIT %(room)s',
                            {**values, "seen": list(seen), "room": room}).fetchall()
        return (rows[0][1] if rows else 0), [row[0] for row in rows]

    def _neighbors(self, conn: psycopg.Connection, layer: list[str], params: dict[str, Any]) -> list[str]:
        """一层节点（同为实体或同为事件）的全部相邻节点 ID。"""
        sql, values = self._neighbor_query(layer, params)
        return [row[0] for row in conn.execute('SELECT node FROM (' + sql + ') neighbors ORDER BY node COLLATE "C"', values)]

    def _assemble(self, conn: psycopg.Connection, kept: list[str], params: dict[str, Any], *, total_nodes: int,
                  truncated: bool, total_complete: bool = True) -> dict[str, Any]:
        entity_ids = [node[4:] for node in kept if node.startswith("ent:")]
        event_ids = [node[4:] for node in kept if node.startswith("evt:")]
        entities = self._db.effective_entities(conn, entity_ids)
        events = self._events_where(conn, "ee.event_id=ANY(%(ids)s)", {"ids": event_ids})
        titles = self._db.titles(conn, {event.version_id for event in events})
        counts: dict[str, tuple[int, int]] = {}
        raw_ids = self._raw_ids(conn, entity_ids)
        for id_, count, documents in conn.execute("""SELECT COALESCE(r.target,m.entity_id),sum(m.n),count(DISTINCT m.version_id)
            FROM (SELECT entity_id,version_id,count(*) AS n FROM mentions WHERE entity_id=ANY(%s)
                  GROUP BY entity_id,version_id) m
            JOIN documents d ON d.version_id=m.version_id AND d.status='published'
            LEFT JOIN entity_redirects r ON r.source=m.entity_id
            GROUP BY COALESCE(r.target,m.entity_id)""", (raw_ids,)):
            counts[id_] = (int(count), documents)
        nodes = {"ent:" + id_: entity_node(entity, *counts.get(id_, (0, 0))) for id_, entity in entities.items()}
        edges: list[dict[str, Any]] = []
        for event in events:
            nodes["evt:" + event.event_id] = event_node(event, titles.get(event.version_id, event.version_id))
            edges.extend(event_edges(event, entities))
        return assemble_entity_graph(nodes, edges, kept, total_nodes=total_nodes, truncated=truncated, total_complete=total_complete)

    # —— 时间轴 ——

    def timeline(self, *, include_review: bool = True, start: date | None = None, end: date | None = None,
                 limit: int = 2000, scope: Visibility | None = None) -> dict[str, Any]:
        return self._scoped_timeline(include_review, start, end, limit, scope)  # 批次 20a：不限范围也走带范围的路径（全部已发布文档）

    # —— 状态与审核列表 ——

    def knowledge_counts(self, *, scope: Visibility | None = None) -> dict[str, Any]:
        return self._scoped_counts(scope)  # 批次 20a：不限范围也走带范围的路径（全部已发布文档）

    def review_entities(self, limit: int = 200, *, scope: Visibility | None = None) -> list[dict[str, Any]]:
        return self._scoped_review_entities(limit, scope)  # 批次 20a：不限范围也走带范围的路径（全部已发布文档）

    def review_events(self, limit: int = 200, *, scope: Visibility | None = None) -> list[dict[str, Any]]:
        return self._scoped_review_events(limit, scope)  # 批次 20a：不限范围也走带范围的路径（全部已发布文档）

    # —— 冲突 ——

    # 按 a 的 ID 升序逐个找同组（同一有效主体、同一规范化行为）中 ID 更大的 b；走 events_pairs 索引，找够即停。
    _PAIRS = """
        SELECT a.event_id, b.event_id
        FROM events a
        JOIN documents da ON da.version_id = a.version_id AND da.status = 'published'
        CROSS JOIN LATERAL (
            SELECT b.event_id FROM events b
            JOIN documents db ON db.version_id = b.version_id AND db.status = 'published'
            WHERE b.eff_subject = a.eff_subject AND b.action_key = a.action_key AND b.event_id > a.event_id
              AND b.eff_verdict = 'pass' AND NOT b.duplicate AND b.version_id <> a.version_id
              AND NOT (a.fingerprint IS NOT NULL AND b.fingerprint IS NOT NULL AND a.fingerprint = b.fingerprint)
              AND NOT EXISTS (SELECT 1 FROM conflicts c WHERE c.pair_id = a.event_id || '|' || b.event_id)
            ORDER BY b.event_id LIMIT %(limit)s) b
        WHERE a.eff_verdict = 'pass' AND NOT a.duplicate AND a.eff_subject IS NOT NULL AND a.action_key <> ''
        ORDER BY a.event_id, b.event_id LIMIT %(limit)s
    """

    def conflict_candidates(self, limit: int) -> list[tuple[EventRecord, EventRecord]]:
        if limit < 1:
            raise ValueError("配对上限必须为正整数")
        with self._db._read() as conn:
            pairs = conn.execute(self._PAIRS, {"limit": limit}).fetchall()
            events = {event.event_id: event for event in self._events_where(conn, "ee.event_id=ANY(%(ids)s)",
                                                                         {"ids": list({id_ for pair in pairs for id_ in pair})})}
            return [(events[a], events[b]) for a, b in pairs]

    def conflict_candidate_count(self, cap: int = CANDIDATE_COUNT_CAP) -> int:
        if cap < 1:
            raise ValueError("计数上限必须为正整数")
        with self._db._read() as conn:
            return conn.execute("SELECT count(*) FROM (" + self._PAIRS + ") pairs", {"limit": cap}).fetchone()[0]

    def conflict_rows(self, pair_id: str | None = None, *, scope: Visibility | None = None) -> list[dict[str, Any]]:
        return self._scoped_conflicts(pair_id, scope)  # 批次 20a：不限范围也走带范围的路径（全部已发布文档）


    def _neighbor_query(self, layer: list[str], params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """相邻节点只查标识列，按实体/事件两个方向走索引。"""
        if layer[0].startswith("ent:"):
            sql = "SELECT ('evt:'||event_id) COLLATE \"C\" AS node FROM (" + _VISIBLE_OF_ENTITIES + ") found"
        else:
            sql = """SELECT DISTINCT ('ent:'||p.id) COLLATE "C" AS node FROM (
                SELECT ee.subject_entity AS id FROM effective_events ee WHERE ee.event_id=ANY(%(ids)s)
                UNION SELECT ee.object_entity FROM effective_events ee WHERE ee.event_id=ANY(%(ids)s)) p
                JOIN entities e ON e.id=p.id"""
        return sql, {**params, "ids": [node[4:] for node in layer]}

    # —— 审核决定 ——

    def entity_names(self) -> list[tuple[str, str]]:
        """批次 18a（架构方添加）：一次读取全部实体的名称与合并去向，不读事件与提及。"""
        with self._db._read() as conn:
            mapping = self._db.redirects(conn)
            names: set[tuple[str, str]] = set()
            for id_, data in conn.execute("SELECT id,data FROM entities"):
                final = mapping.get(id_, id_)
                for name in (data["canonical"], *data.get("aliases", [])):
                    names.add((name, final))
        return sorted(names)

    def review_context(self, kind: DecisionKind, target: str, value: str | None) -> ReviewContext:
        with self._db._read() as conn:
            decisions = self._db.decisions(conn)
            if kind is DecisionKind.RELATION_REJECT:                       # 批次 23b
                relations = [RelationRecord.from_json(row[0]) for row in
                             conn.execute("SELECT data FROM entity_relations WHERE relation_id=%s", (target,))]
                ids = {id_ for r in relations for id_ in (r.source_entity, r.target_entity)}
                return ReviewContext(entities=self._db.entities(conn, ids), relations=relations, decisions=decisions)
            if kind in (DecisionKind.ENTITY_MERGE, DecisionKind.ENTITY_DISTINCT, DecisionKind.ENTITY_SUBTYPE):
                ids = self._db.decision_entity_ids(decisions) | {target}
                if value is not None and kind is not DecisionKind.ENTITY_SUBTYPE:
                    ids.add(value)
                return ReviewContext(entities=self._db.entities(conn, ids), decisions=decisions)
            if kind in (DecisionKind.EVENT_PASS, DecisionKind.EVENT_REJECT):
                return ReviewContext(events=self._db.raw_events(conn, [target]), decisions=decisions)
            if kind is DecisionKind.REFERENCE_SET:
                references = [ReferenceRecord.from_json(row[0]) for row in conn.execute("SELECT data FROM doc_references WHERE ref_id=%s", (target,))]
                versions = {row.version_id for row in references}
                if value is not None:
                    versions.add(value)
                documents = dict(conn.execute("SELECT version_id,title FROM documents WHERE version_id=ANY(%s) AND status='published'", (list(versions),)))
                return ReviewContext(references=references, documents=documents, decisions=decisions)
            if kind in (DecisionKind.CONFLICT_CONFIRM, DecisionKind.CONFLICT_DISMISS):
                return ReviewContext(conflicts=self.conflict_rows(target), decisions=decisions)
            if kind is DecisionKind.QUESTION_DONE:
                questions = [{"time": time, "question": question, "status": status} for time, question, status in
                             conn.execute("SELECT time,question,status FROM unanswered WHERE time||'|'||question=%s ORDER BY id", (target,))]
                return ReviewContext(questions=questions, decisions=decisions)
            return ReviewContext(decisions=decisions)


def open_knowledge(dsn: str, *, schema: str = "public") -> tuple[PgKnowledgeDb, PgKnowledgeStore, PgLinkStore,
                                                                    PgDecisionStore, PgKnowledgeQueries]:
    db = PgKnowledgeDb(dsn, schema=schema)
    return db, PgKnowledgeStore(db), PgLinkStore(db), PgDecisionStore(db), PgKnowledgeQueries(db)
