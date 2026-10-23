"""ScanIndex 的 PostgreSQL 实现（批次 15d）。表结构见 pg_migrations/004_import.sql，语义见 core/app/scan.py。

- 每个方法一个短事务；实例内用锁串行化连接；连接断开后下次调用重连。
- put 用 INSERT … ON CONFLICT (root, path) DO UPDATE，一次调用一条语句批量写入（executemany 或 UNNEST 均可）。
- 构造时表不存在 → ValueError（提示先迁移）。
"""
from __future__ import annotations

from collections.abc import Collection, Sequence
from threading import RLock
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from core.adapters.pg_registry import _connect
from core.domain.scan import ScanEntry


class PgScanIndex:
    def __init__(self, dsn: str, *, schema: str = "public") -> None:
        self._dsn, self._schema = dsn, schema
        self._lock = RLock()
        self._connection = _connect(dsn, schema)
        try:
            with self._connection.transaction():
                self._connection.execute("SELECT 1 FROM scan_files LIMIT 0")
        except psycopg.errors.UndefinedTable as exc:
            self._connection.close()
            raise ValueError("数据库尚未迁移到目录扫描结构，请先运行迁移") from exc

    @property
    def _conn(self) -> psycopg.Connection:
        if self._connection.closed or self._connection.broken:
            self._connection = _connect(self._dsn, self._schema)
        return self._connection

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def entries(self, root: str) -> dict[str, ScanEntry]:
        with self._lock, self._conn.transaction():
            rows = self._conn.execute(
                "SELECT path,size,mtime_ns,sha256,version_id,outcome FROM scan_files WHERE root=%s ORDER BY path",
                (root,)).fetchall()
            return {row[0]: ScanEntry(*row) for row in rows}

    def put(self, root: str, entries: Sequence[ScanEntry]) -> None:
        if not entries:
            return
        with self._lock, self._conn.transaction(), self._conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO scan_files(root,path,size,mtime_ns,sha256,version_id,outcome) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (root,path) DO UPDATE SET "
                "size=EXCLUDED.size,mtime_ns=EXCLUDED.mtime_ns,sha256=EXCLUDED.sha256,"
                "version_id=EXCLUDED.version_id,outcome=EXCLUDED.outcome",
                [(root, entry.path, entry.size, entry.mtime_ns, entry.sha256, entry.version_id, entry.outcome)
                 for entry in entries])

    def remove(self, root: str, paths: Collection[str]) -> None:
        with self._lock, self._conn.transaction():
            self._conn.execute("DELETE FROM scan_files WHERE root=%s AND path=ANY(%s)", (root, list(paths)))

    def save_run(self, report: dict[str, Any]) -> None:
        with self._lock, self._conn.transaction():
            self._conn.execute(
                "INSERT INTO scan_runs(scan_id,root,started_at,report) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (scan_id) DO UPDATE SET root=EXCLUDED.root,started_at=EXCLUDED.started_at,"
                "report=EXCLUDED.report",
                (report["scan_id"], report["root"], report["started_at"], Jsonb(report)))

    def runs(self, root: str, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit 必须为正数")
        with self._lock, self._conn.transaction():
            rows = self._conn.execute(
                "SELECT report FROM scan_runs WHERE root=%s ORDER BY started_at DESC,scan_id LIMIT %s",
                (root, limit)).fetchall()
            return [row[0] for row in rows]

    def run(self, scan_id: str) -> dict[str, Any] | None:
        with self._lock, self._conn.transaction():
            row = self._conn.execute("SELECT report FROM scan_runs WHERE scan_id=%s", (scan_id,)).fetchone()
            return row[0] if row is not None else None
