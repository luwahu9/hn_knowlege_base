"""DocumentRegistry 的 PostgreSQL 实现（psycopg 3）。规格见 docs/spec-24-registry.md 与 core/app/registry.py。

- 结构由 core/adapters/pg_migrations/*.sql 按文件名顺序迁移；migrate() 幂等。
- 一个 schema 即一个知识库；测试用独立 schema 隔离。
- 进程内保留文档记录与已发布分块的缓存（与 KbStore 相同的读路径），所有写入先提交数据库再更新缓存。
  本批次只支持一个写入进程；其他进程（如迁移工具）写入后需重启 Web 服务。
- 追加事件时持有事务级咨询锁，保证 seq 的提交顺序与数值顺序一致，订阅者不会漏读。
"""
from __future__ import annotations

import copy
from collections import Counter
from collections.abc import Iterable, Mapping
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from core.app.ports import Chunk, PermanentError
from core.domain.purge import PURGEABLE, PURGED, tombstone
from core.app.registry import (DOCUMENT_PUBLISHED, DOCUMENT_REMOVED, EVENT_KINDS, check_status_change, check_switch,
                               page_documents, stats_of, status_event)
from core.domain.permissions import Visibility
from core.app.retrieval import ChunkLookup
from core.domain.folders import folder_ancestors, folder_name, normalize_folder_path

MIGRATIONS = Path(__file__).with_name("pg_migrations")
_EVENT_LOCK = 0x4841494E41  # "HAINA"
_SCHEMA = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_META_KEYS = ("dataset_id", "dataset_name", "group_tag")


def _connect(dsn: str, schema: str, *, autocommit: bool = False) -> psycopg.Connection:
    if not isinstance(schema, str) or _SCHEMA.fullmatch(schema) is None:
        raise ValueError("数据库 schema 名称无效")
    return psycopg.connect(dsn, autocommit=autocommit, options=f"-c search_path={schema}")


def migrate(dsn: str, *, schema: str = "public") -> list[str]:
    """创建 schema（如不存在）并执行尚未执行的迁移，返回本次执行的文件名列表。"""
    applied: list[str] = []
    with _connect(dsn, schema) as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_EVENT_LOCK,))
            conn.execute(psycopg.sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(psycopg.sql.Identifier(schema)))
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (_EVENT_LOCK,))
                if conn.execute("SELECT 1 FROM schema_migrations WHERE filename=%s", (path.name,)).fetchone():
                    continue
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations(filename) VALUES (%s)", (path.name,))
                applied.append(path.name)
    return applied


def initialize(dsn: str, *, schema: str = "public", dataset_id: str, dataset_name: str, group_tag: str) -> None:
    """写入知识库元数据（RAGFlow 数据集与分组标签）。已有不同的值 → ValueError，不覆盖。"""
    values = {"dataset_id": dataset_id, "dataset_name": dataset_name, "group_tag": group_tag}
    with _connect(dsn, schema) as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_EVENT_LOCK,))
        current = dict(conn.execute("SELECT key,value FROM kb_meta").fetchall())
        if any(key in current and current[key] != value for key, value in values.items()):
            raise ValueError("不得覆盖已有知识库元数据")
        for key, value in values.items():
            conn.execute("INSERT INTO kb_meta(key,value) VALUES (%s,%s) ON CONFLICT (key) DO NOTHING", (key, value))


class PgRegistry:
    """批次 15b 起：文档记录缓存在进程内并按修订号（documents.rev）增量刷新，其他进程的写入在下一次读取时可见；
    分块不再缓存，按需查询。"""

    def __init__(self, dsn: str, *, schema: str = "public") -> None:
        self._dsn, self._schema = dsn, schema
        self._lock = RLock()
        self._connection = _connect(dsn, schema)
        self._documents: dict[str, dict[str, Any]] = {}
        self._rev = 0
        try:
            with self._connection.transaction():
                self._meta = dict(self._connection.execute("SELECT key,value FROM kb_meta").fetchall())
                if any(key not in self._meta for key in _META_KEYS):
                    raise ValueError("数据库尚未初始化，请先运行迁移工具")
                self._refresh()
            self.startup_versions = frozenset(self._documents)
        except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn) as exc:
            self._connection.close()
            raise ValueError("数据库尚未迁移，请先运行迁移工具") from exc
        except Exception:
            self._connection.close()
            raise

    def _refresh(self) -> None:
        """读取修订号大于已知值的文档记录：新版本按 seq 顺序追加，已有版本原位替换。"""
        with self._lock, self._conn.transaction():
            rows = self._conn.execute("SELECT version_id,record,rev FROM documents WHERE rev>%s ORDER BY seq", (self._rev,)).fetchall()
            for version, record, rev in rows:
                self._documents[version] = record
                self._rev = max(self._rev, rev)

    def _check_dataset(self) -> None:
        """批次 21b：在写事务中调用。库中的检索数据集与本进程启动时读取的不同 → PermanentError（锁住该行直到事务结束，
        与 switch_index 互斥）。"""
        row = self._conn.execute("SELECT value FROM kb_meta WHERE key='dataset_id' FOR SHARE").fetchone()
        if row is None or row[0] != self._meta["dataset_id"]:
            raise PermanentError("检索数据集已切换，请重启进程")

    @property
    def _conn(self) -> psycopg.Connection:
        """数据库重启后连接会断开：下一次操作时重连一次（缓存仍有效，因为只有本进程写入）。"""
        if self._connection.closed or self._connection.broken:
            self._connection = _connect(self._dsn, self._schema)
        return self._connection

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @property
    def dataset_id(self) -> str:
        return self._meta["dataset_id"]

    @property
    def dataset_name(self) -> str:
        return self._meta["dataset_name"]

    @property
    def group_tag(self) -> str:
        return self._meta["group_tag"]

    # —— 文档 ——

    def documents(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh()
            return copy.deepcopy(list(self._documents.values()))

    def detail(self, version_id: str) -> dict[str, Any] | None:
        with self._lock, self._conn.transaction():
            self._refresh()
            record = self._documents.get(version_id)
            if record is None:
                return None
            chunks = ([row[0] for row in self._conn.execute("SELECT row FROM chunks WHERE version_id=%s ORDER BY ordinal", (version_id,))]
                      if record["status"] == "published" else [])
            return copy.deepcopy({"document": record, "chunks": chunks})

    def save_document(self, record: dict[str, Any], chunks: list[dict[str, Any]]) -> None:
        self._save(record, chunks, emit=True)

    def import_document(self, record: dict[str, Any], chunks: list[dict[str, Any]]) -> None:
        """迁移专用：与 save_document 相同，但不产生 document_published 事件（历史文档已抽取过）。"""
        self._save(record, chunks, emit=False)

    def import_unanswered(self, rows: list[dict[str, str]]) -> None:
        """迁移专用：按原时间整体导入未收录问题；目标已有记录 → ValueError，不写入。"""
        rows = copy.deepcopy(rows)
        with self._lock, self._conn.transaction():
            if self._conn.execute("SELECT 1 FROM unanswered LIMIT 1").fetchone():
                raise ValueError("未收录问题目标表非空，不得重复导入")
            for row in rows:
                self._conn.execute("INSERT INTO unanswered(time,question,status) VALUES (%s,%s,%s)",
                                   (row["time"], row["question"], row["status"]))

    def _save(self, record: dict[str, Any], chunks: list[dict[str, Any]], *, emit: bool) -> None:
        record = copy.deepcopy(record)
        version = record["version_id"]
        if not isinstance(version, str) or re.fullmatch(r"v\d+_[0-9a-fA-F]{10}", version) is None:
            raise ValueError("文档版本号格式无效")
        record["folder"] = normalize_folder_path(record.get("folder", ""))
        with self._lock:
            try:
                with self._conn.transaction():
                    self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_EVENT_LOCK,))
                    self._check_dataset()
                    if self._conn.execute("SELECT 1 FROM documents WHERE version_id=%s", (version,)).fetchone():
                        raise ValueError("不得覆盖已有文档记录")
                    folder_id = self._ensure(record["folder"])
                    self._conn.execute("INSERT INTO documents(version_id,folder_id,status,title,content_hash,imported_at,record) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (version, folder_id, record["status"], record["title"], record.get("content_hash"), record.get("imported_at"), Jsonb(record)))
                    if record["status"] == "published":
                        with self._conn.cursor().copy("COPY chunks(version_id,ordinal,chunk_id,row) FROM STDIN") as writer:
                            for ordinal, row in enumerate(chunks):
                                writer.write_row((version, ordinal, row["chunk_id"], Jsonb(row)))
                        if emit:
                            self._insert_event(DOCUMENT_PUBLISHED, version, {"folder": record["folder"], "title": record["title"]})
            except psycopg.errors.UniqueViolation as exc:
                if exc.diag.constraint_name == "documents_version_id_key":
                    raise ValueError("不得覆盖已有文档记录") from exc
                raise
            self._refresh()

    def set_status(self, version_id: str, status: str, entry: dict[str, Any], *,
                   chunk_map: Mapping[str, str] | None = None, engine_doc_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            with self._conn.transaction():
                self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_EVENT_LOCK,))
                self._check_dataset()
                row = self._conn.execute("SELECT record FROM documents WHERE version_id=%s FOR UPDATE",
                                         (version_id,)).fetchone()
                if row is None:
                    raise KeyError(version_id)
                record = row[0]
                chunks = [(ordinal, data) for ordinal, data in self._conn.execute(
                    "SELECT ordinal,row FROM chunks WHERE version_id=%s ORDER BY ordinal", (version_id,))]
                check_status_change(record["status"], status, (data["chunk_id"] for _, data in chunks), chunk_map,
                                    engine_doc_id)
                record["status"] = status
                record["lifecycle"] = [*record.get("lifecycle", []), copy.deepcopy(entry)]
                if status == "published":
                    record["engine_doc_id"] = engine_doc_id
                    for ordinal, data in chunks:
                        data["engine_chunk_id"] = chunk_map[data["chunk_id"]]
                        self._conn.execute("UPDATE chunks SET row=%s WHERE version_id=%s AND ordinal=%s",
                                           (Jsonb(data), version_id, ordinal))
                self._conn.execute("UPDATE documents SET status=%s, record=%s WHERE version_id=%s",
                                   (status, Jsonb(record), version_id))
                kind, payload = status_event(record, status)
                self._insert_event(kind, version_id, payload)
            self._refresh()
            return copy.deepcopy(record)

    def switch_index(self, dataset_id: str, dataset_name: str,
                     mapping: Mapping[str, tuple[str, Mapping[str, str]]]) -> None:
        """批量更新：映射先 COPY 进临时表，再各用一条 UPDATE 写入分块与文档（文档数万、分块百万级时仍在一个事务内完成）。"""
        with self._lock:
            with self._conn.transaction():
                self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_EVENT_LOCK,))
                self._conn.execute("SELECT value FROM kb_meta WHERE key IN ('dataset_id','dataset_name') FOR UPDATE")
                published: dict[str, list[str]] = {}
                for version, chunk_id in self._conn.execute(
                        "SELECT d.version_id, c.chunk_id FROM documents d LEFT JOIN chunks c USING (version_id) "
                        "WHERE d.status='published' ORDER BY d.version_id, c.ordinal"):
                    published.setdefault(version, [])
                    if chunk_id is not None:
                        published[version].append(chunk_id)
                check_switch(dataset_id, dataset_name, published, mapping)
                self._conn.execute("CREATE TEMP TABLE switch_chunks (version_id text, chunk_id text, engine_chunk_id text) "
                                   "ON COMMIT DROP")
                self._conn.execute("CREATE TEMP TABLE switch_docs (version_id text, engine_doc_id text) ON COMMIT DROP")
                with self._conn.cursor().copy("COPY switch_chunks FROM STDIN") as writer:
                    for version, (_, chunk_map) in mapping.items():
                        for chunk_id, engine_id in chunk_map.items():
                            writer.write_row((version, chunk_id, engine_id))
                with self._conn.cursor().copy("COPY switch_docs FROM STDIN") as writer:
                    for version, (engine_doc_id, _) in mapping.items():
                        writer.write_row((version, engine_doc_id))
                self._conn.execute(
                    "UPDATE chunks c SET row = jsonb_set(c.row, '{engine_chunk_id}', to_jsonb(m.engine_chunk_id)) "
                    "FROM switch_chunks m WHERE c.version_id = m.version_id AND c.chunk_id = m.chunk_id")
                self._conn.execute(
                    "UPDATE documents d SET record = jsonb_set(d.record, '{engine_doc_id}', to_jsonb(s.engine_doc_id)) "
                    "FROM switch_docs s WHERE d.version_id = s.version_id")
                self._conn.execute("UPDATE kb_meta SET value=%s WHERE key='dataset_id'", (dataset_id,))
                self._conn.execute("UPDATE kb_meta SET value=%s WHERE key='dataset_name'", (dataset_name,))
            self._meta = {**self._meta, "dataset_id": dataset_id, "dataset_name": dataset_name}
            self._refresh()

    def purge(self, version_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            with self._conn.transaction():
                self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_EVENT_LOCK,))
                self._check_dataset()
                row = self._conn.execute("SELECT record FROM documents WHERE version_id=%s FOR UPDATE",
                                         (version_id,)).fetchone()
                if row is None:
                    raise KeyError(version_id)
                if row[0]["status"] not in PURGEABLE:
                    raise ValueError("只能彻底删除已撤下或已替代的文档")
                stone = tombstone(row[0], entry)
                self._conn.execute("DELETE FROM chunks WHERE version_id=%s", (version_id,))
                self._conn.execute("UPDATE documents SET status=%s, content_hash=NULL, record=%s WHERE version_id=%s",
                                   (PURGED, Jsonb(stone), version_id))
                self._insert_event(DOCUMENT_REMOVED, version_id, {"folder": stone["folder"], "title": stone["title"],
                                                                  "status": PURGED})
            self._refresh()
            return copy.deepcopy(stone)

    def stored_chunks(self, version_id: str) -> list[dict[str, Any]]:
        with self._lock, self._conn.transaction():
            return [row[0] for row in self._conn.execute(
                "SELECT row FROM chunks WHERE version_id=%s ORDER BY ordinal", (version_id,))]

    def next_version_number(self) -> int:
        with self._lock, self._conn.transaction():
            self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_EVENT_LOCK,))
            maximum = self._conn.execute(
                "SELECT COALESCE(max(substring(version_id from '^v([0-9]+)_')::bigint),0) FROM documents"
            ).fetchone()[0]
            number = self._conn.execute("SELECT nextval('version_number_seq')").fetchone()[0]
            if number <= maximum:
                self._conn.execute("SELECT setval('version_number_seq',%s)", (maximum,))
                number = self._conn.execute("SELECT nextval('version_number_seq')").fetchone()[0]
            return number

    def set_review(self, version_id: str, review: dict[str, Any] | None) -> dict[str, Any]:
        with self._lock:
            with self._conn.transaction():
                self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_EVENT_LOCK,))
                if review is None:
                    row = self._conn.execute("UPDATE documents SET record=record-'review' WHERE version_id=%s "
                                             "RETURNING record", (version_id,)).fetchone()
                else:
                    row = self._conn.execute(
                        "UPDATE documents SET record=jsonb_set(record,'{review}',%s) WHERE version_id=%s RETURNING record",
                        (Jsonb(copy.deepcopy(review)), version_id)).fetchone()
                if row is None:
                    raise KeyError(version_id)
            self._refresh()
            return copy.deepcopy(row[0])

    def document_page(self, scope: Visibility | None = None, *, status: str | None = None, query: str | None = None,
                      offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """在本实例的记录缓存上筛选（按修订号增量刷新，见 _refresh），只深拷贝返回的那一页。"""
        with self._lock:
            self._refresh()
            return page_documents(self._documents.values(), scope, status=status, query=query, offset=offset,
                                  limit=limit)

    def document_stats(self, scope: Visibility | None = None) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            return stats_of(self._documents.values(), scope)

    def folder_counts(self) -> dict[str, int]:
        with self._lock:
            self._refresh()
            return dict(Counter(row.get("folder", "") for row in self._documents.values()))

    def folders_of(self, version_ids: Iterable[str]) -> dict[str, str]:
        with self._lock:
            self._refresh()
            return {version: self._documents[version].get("folder", "") for version in set(version_ids)
                    if version in self._documents}

    def records(self, version_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """批次 18a（架构方添加）：在本实例的记录缓存上取，只深拷贝给出的版本。"""
        with self._lock:
            self._refresh()
            return {version: copy.deepcopy(self._documents[version]) for version in set(version_ids)
                    if version in self._documents}

    def find_by_digest(self, sha256: str) -> str | None:
        """用 004 迁移的 documents_digest 索引（right(version_id, 10)）。"""
        if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None:
            raise ValueError("文件哈希必须是 64 位十六进制")
        with self._lock, self._conn.transaction():
            row = self._conn.execute(
                "SELECT version_id FROM documents WHERE right(version_id, 10)=%s AND status<>'purged' ORDER BY seq LIMIT 1",
                (sha256[:10].lower(),)).fetchone()
            return row[0] if row is not None else None

    def find_by_hash(self, content_hash: str) -> str | None:
        with self._lock, self._conn.transaction():
            row = self._conn.execute("SELECT version_id FROM documents WHERE status='published' AND content_hash=%s ORDER BY seq LIMIT 1", (content_hash,)).fetchone()
            return row[0] if row else None

    def context(self) -> tuple[ChunkLookup, dict[str, str], dict[str, str]]:
        """分块目录改为按需查询（_PgChunkLookup），不再把全部分块读进内存。"""
        with self._lock:
            self._refresh()
            return (_PgChunkLookup(self), {v: r["title"] for v, r in self._documents.items()},
                    {v: r["filename"] for v, r in self._documents.items()})

    def lookup_chunk(self, version_id: str, engine_chunk_id: str) -> Chunk | None:
        with self._lock, self._conn.transaction():
            row = self._conn.execute("SELECT c.row FROM chunks c JOIN documents d USING(version_id) WHERE c.version_id=%s AND c.row->>'engine_chunk_id'=%s AND d.status='published' LIMIT 1", (version_id, engine_chunk_id)).fetchone()
            if row is None:
                return None
            data = row[0]
            return Chunk(data["chunk_id"], data["text"], data["page_start"], data["page_end"], data["section_path"], data["meta"])

    def last_event_seq(self) -> int:
        with self._lock, self._conn.transaction():
            return self._conn.execute("SELECT COALESCE(MAX(seq),0) FROM knowledge_events").fetchone()[0]

    # —— 未收录问题 ——

    def record_unanswered(self, question: str, status: str) -> None:
        with self._lock, self._conn.transaction():
            self._conn.execute("INSERT INTO unanswered(time,question,status) VALUES (%s,%s,%s)", (_now(), question, status))

    def unanswered(self) -> list[dict[str, str]]:
        with self._lock, self._conn.transaction():
            return [{"time": time, "question": question, "status": status}
                    for time, question, status in self._conn.execute("SELECT time,question,status FROM unanswered ORDER BY id")]

    # —— 文件夹 ——

    def ensure_folder(self, path: str) -> str:
        path = normalize_folder_path(path)
        with self._lock, self._conn.transaction():
            self._ensure(path)
        return path

    def _ensure(self, path: str) -> int:
        """在当前事务中建立 path 及其祖先，返回 path 的 id。已存在的文件夹只读不写。"""
        row = self._conn.execute("SELECT id FROM folders WHERE path=%s", (path,)).fetchone()
        if row:
            return row[0]
        parent = self._conn.execute("SELECT id FROM folders WHERE path=''").fetchone()[0]
        for ancestor in folder_ancestors(path):
            row = self._conn.execute("SELECT id FROM folders WHERE path=%s", (ancestor,)).fetchone()
            if row:
                parent = row[0]
                continue
            row = self._conn.execute("INSERT INTO folders(path,parent_id,name) VALUES (%s,%s,%s) ON CONFLICT(path) DO NOTHING RETURNING id", (ancestor, parent, folder_name(ancestor))).fetchone()
            parent = row[0] if row else self._conn.execute("SELECT id FROM folders WHERE path=%s", (ancestor,)).fetchone()[0]
        return parent

    def folders(self) -> list[str]:
        with self._lock, self._conn.transaction():
            return sorted(row[0] for row in self._conn.execute("SELECT path FROM folders WHERE path<>''"))

    # —— 知识事件 ——

    def append_event(self, kind: str, version_id: str | None = None, payload: dict[str, Any] | None = None) -> int:
        if kind not in EVENT_KINDS:
            raise ValueError("未知知识事件类型")
        with self._lock, self._conn.transaction():
            return self._insert_event(kind, version_id, copy.deepcopy(payload) if payload is not None else {})

    def _insert_event(self, kind: str, version_id: str | None, payload: dict[str, Any]) -> int:
        self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_EVENT_LOCK,))
        return self._conn.execute("INSERT INTO knowledge_events(kind,version_id,payload,time) VALUES (%s,%s,%s,%s) RETURNING seq",
                                  (kind, version_id, Jsonb(payload), _now())).fetchone()[0]

    def events(self, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("事件读取条数必须大于零")
        with self._lock, self._conn.transaction():
            return [{"seq": seq, "kind": kind, "version_id": version, "payload": payload, "time": time}
                    for seq, kind, version, payload, time in self._conn.execute(
                        "SELECT seq,kind,version_id,payload,time FROM knowledge_events WHERE seq>%s ORDER BY seq LIMIT %s", (after, limit))]

    def offset(self, subscriber: str) -> int:
        with self._lock, self._conn.transaction():
            row = self._conn.execute("SELECT seq FROM event_offsets WHERE subscriber=%s", (subscriber,)).fetchone()
            return row[0] if row else 0

    def commit_offset(self, subscriber: str, seq: int) -> None:
        with self._lock, self._conn.transaction():
            self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_EVENT_LOCK,))
            row = self._conn.execute("SELECT seq FROM event_offsets WHERE subscriber=%s", (subscriber,)).fetchone()
            current = row[0] if row else 0
            maximum = self._conn.execute("SELECT COALESCE(MAX(seq),0) FROM knowledge_events").fetchone()[0]
            if seq < current or seq > maximum:
                raise ValueError("订阅进度不能后退或超过最大事件序号")
            if seq != current:
                self._conn.execute("INSERT INTO event_offsets(subscriber,seq) VALUES (%s,%s) ON CONFLICT(subscriber) DO UPDATE SET seq=EXCLUDED.seq", (subscriber, seq))


class _PgChunkLookup:
    def __init__(self, registry: PgRegistry) -> None:
        self._registry = registry

    def lookup(self, version_id: str, engine_chunk_id: str) -> Chunk | None:
        return self._registry.lookup_chunk(version_id, engine_chunk_id)


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()
