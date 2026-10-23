"""RebuildRepo 的 PostgreSQL 实现（批次 21b）。表结构见 pg_migrations/011_index_rebuild.sql。

每个方法一个短事务；实例内用锁串行化连接；连接断开后下次调用重连。构造时表不存在 → ValueError（提示先迁移）。"""
from __future__ import annotations

from collections.abc import Mapping
from threading import RLock
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from core.adapters.pg_registry import _connect
from core.app.reindex import BuiltDoc, Rebuild

_COLUMNS = ("rebuild_id,dataset_id,dataset_name,embedding_model,status,created_at,created_by,revision,compare,"
            "compare_revision,switched_at,previous_dataset_id")
_FIELDS = {"status", "compare", "compare_revision", "switched_at", "previous_dataset_id"}


def _rebuild(row: tuple[Any, ...]) -> Rebuild:
    return Rebuild(*row)


class PgRebuildRepo:
    def __init__(self, dsn: str, *, schema: str = "public") -> None:
        self._dsn, self._schema = dsn, schema
        self._lock = RLock()
        self._connection = _connect(dsn, schema)
        try:
            with self._connection.transaction():
                self._connection.execute("SELECT 1 FROM index_rebuilds LIMIT 0")
        except psycopg.errors.UndefinedTable as exc:
            self._connection.close()
            raise ValueError("数据库尚未迁移到重建记录结构，请先运行迁移") from exc

    @property
    def _conn(self) -> psycopg.Connection:
        if self._connection.closed or self._connection.broken:
            self._connection = _connect(self._dsn, self._schema)
        return self._connection

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def rebuilds(self) -> list[Rebuild]:
        with self._lock, self._conn.transaction():
            return [_rebuild(row) for row in self._conn.execute(f"SELECT {_COLUMNS} FROM index_rebuilds ORDER BY seq")]

    def get(self, rebuild_id: str) -> Rebuild | None:
        with self._lock, self._conn.transaction():
            row = self._conn.execute(f"SELECT {_COLUMNS} FROM index_rebuilds WHERE rebuild_id=%s", (rebuild_id,)).fetchone()
            return _rebuild(row) if row is not None else None

    def create(self, rebuild: Rebuild) -> None:
        values = (rebuild.rebuild_id, rebuild.dataset_id, rebuild.dataset_name, rebuild.embedding_model, rebuild.status,
                  rebuild.created_at, rebuild.created_by, rebuild.revision,
                  Jsonb(dict(rebuild.compare)) if rebuild.compare is not None else None, rebuild.compare_revision,
                  rebuild.switched_at, rebuild.previous_dataset_id)
        with self._lock:
            try:
                with self._conn.transaction():
                    self._conn.execute(f"INSERT INTO index_rebuilds({_COLUMNS}) VALUES ({','.join(['%s'] * 12)})", values)
            except psycopg.errors.UniqueViolation as exc:
                raise ValueError("重建记录或数据集已登记，或已有正在进行的重建") from exc

    def update(self, rebuild_id: str, **fields: Any) -> Rebuild:
        unknown = set(fields) - _FIELDS
        if unknown:
            raise ValueError("不能修改这些字段：" + ", ".join(sorted(unknown)))
        with self._lock:
            try:
                with self._conn.transaction():
                    row = None
                    if fields:
                        names = sorted(fields)
                        values = [Jsonb(dict(fields[n])) if n == "compare" and fields[n] is not None else fields[n]
                                  for n in names]
                        row = self._conn.execute(
                            f"UPDATE index_rebuilds SET {', '.join(f'{n}=%s' for n in names)} WHERE rebuild_id=%s "
                            f"RETURNING {_COLUMNS}", (*values, rebuild_id)).fetchone()
                    else:
                        row = self._conn.execute(f"SELECT {_COLUMNS} FROM index_rebuilds WHERE rebuild_id=%s",
                                                 (rebuild_id,)).fetchone()
            except psycopg.errors.UniqueViolation as exc:
                raise ValueError("已有正在进行的重建") from exc
        if row is None:
            raise KeyError(rebuild_id)
        return _rebuild(row)

    def docs(self, rebuild_id: str) -> dict[str, BuiltDoc]:
        with self._lock, self._conn.transaction():
            return {version: BuiltDoc(engine_doc_id, chunk_map, signature) for version, engine_doc_id, chunk_map, signature
                    in self._conn.execute("SELECT version_id,engine_doc_id,chunk_map,signature FROM index_rebuild_docs "
                                          "WHERE rebuild_id=%s ORDER BY version_id", (rebuild_id,))}

    def _bump(self, rebuild_id: str) -> None:
        if self._conn.execute("UPDATE index_rebuilds SET revision=revision+1 WHERE rebuild_id=%s RETURNING 1",
                              (rebuild_id,)).fetchone() is None:
            raise KeyError(rebuild_id)

    def put_doc(self, rebuild_id: str, version_id: str, doc: BuiltDoc) -> None:
        with self._lock, self._conn.transaction():
            self._bump(rebuild_id)
            self._conn.execute(
                "INSERT INTO index_rebuild_docs(rebuild_id,version_id,engine_doc_id,chunk_map,signature) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (rebuild_id,version_id) DO UPDATE SET engine_doc_id=EXCLUDED.engine_doc_id, "
                "chunk_map=EXCLUDED.chunk_map, signature=EXCLUDED.signature",
                (rebuild_id, version_id, doc.engine_doc_id, Jsonb(dict(doc.chunk_map)), doc.signature))

    def drop_doc(self, rebuild_id: str, version_id: str) -> None:
        with self._lock, self._conn.transaction():
            self._bump(rebuild_id)
            self._conn.execute("DELETE FROM index_rebuild_docs WHERE rebuild_id=%s AND version_id=%s", (rebuild_id, version_id))

    def replace_docs(self, rebuild_id: str, docs: Mapping[str, BuiltDoc]) -> None:
        with self._lock, self._conn.transaction():
            self._bump(rebuild_id)
            self._conn.execute("DELETE FROM index_rebuild_docs WHERE rebuild_id=%s", (rebuild_id,))
            with self._conn.cursor().copy(
                    "COPY index_rebuild_docs(rebuild_id,version_id,engine_doc_id,chunk_map,signature) FROM STDIN") as writer:
                for version, doc in docs.items():
                    writer.write_row((rebuild_id, version, doc.engine_doc_id, Jsonb(dict(doc.chunk_map)), doc.signature))
