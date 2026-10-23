"""MailCatalog 的 PostgreSQL 实现（批次 17a）。表结构见 pg_migrations/007_mail.sql，语义见 core/app/mail.py。

- 每个方法一个短事务；实例内用锁串行化连接；连接断开后下次调用重连。构造时表不存在 → ValueError（提示先迁移）。
- 子树条件不用 LIKE：dir 等于 d，或 d || '/' <= dir < d || '0'（"C" 排序下 '/' 之后紧接 '0'），走主键的范围扫描；
  d 为 '' 时是整个根，不加条件（在 Python 中分别拼接，不写成 "d = '' OR …"，以免通用执行计划用不上范围扫描）。
- put_files 一次调用一条语句批量写入（UNNEST），按 (root, dir, name) ON CONFLICT DO UPDATE。
- tree_mail_ids 按 (dir, name) 键集分页读取（每页 batch 行），不一次取出整个子树，也不持有长事务。
- folder_counts 只读 mail_dirs，在 SQL 中截取前 depth 段并汇总。
"""
from __future__ import annotations

from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from threading import RLock
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from core.adapters.pg_registry import _connect
from core.app.mail import BULK, DirRecord, FileRecord

class PgMailCatalog:
    def __init__(self, dsn: str, *, schema: str = "public") -> None:
        self._dsn, self._schema = dsn, schema
        self._lock = RLock()
        self._connection = _connect(dsn, schema)
        try:
            with self._connection.transaction():
                self._connection.execute("SELECT 1 FROM mail_files LIMIT 0")
        except psycopg.errors.UndefinedTable as exc:
            self._connection.close()
            raise ValueError("数据库尚未迁移到邮件结构，请先运行迁移") from exc

    @property
    def _conn(self) -> psycopg.Connection:
        if self._connection.closed or self._connection.broken:
            self._connection = _connect(self._dsn, self._schema)
        return self._connection

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def dir(self, root: str, dir: str) -> DirRecord | None:
        with self._tx() as conn:
            row = conn.execute("SELECT mtime_ns,children,mails FROM mail_dirs WHERE root=%s AND dir=%s", (root, dir)).fetchone()
            return DirRecord(dir, row[0], tuple(row[1]), row[2]) if row else None

    def dirs(self, root: str, dirs: Collection[str]) -> dict[str, DirRecord]:
        """批次 17a 修正（架构方添加）：一条按主键查找的语句批量取目录记录。"""
        if not dirs:
            return {}
        with self._tx() as conn:
            rows = conn.execute("SELECT dir,mtime_ns,children,mails FROM mail_dirs WHERE root=%s AND dir=ANY(%s)",
                                (root, list(dirs))).fetchall()
            return {row[0]: DirRecord(row[0], row[1], tuple(row[2]), row[3]) for row in rows}

    def put_dir(self, root: str, record: DirRecord) -> None:
        with self._tx() as conn:
            conn.execute("""INSERT INTO mail_dirs(root,dir,mtime_ns,children,mails) VALUES (%s,%s,%s,%s,%s)
                            ON CONFLICT(root,dir) DO UPDATE SET mtime_ns=EXCLUDED.mtime_ns,
                                children=EXCLUDED.children,mails=EXCLUDED.mails""",
                         (root, record.dir, record.mtime_ns, list(record.children), record.mails))

    def files(self, root: str, dir: str) -> dict[str, FileRecord]:
        with self._tx() as conn:
            return {name: FileRecord(name, size, mtime, mail) for name, size, mtime, mail in conn.execute(
                "SELECT name,size,mtime_ns,mail_id FROM mail_files WHERE root=%s AND dir=%s ORDER BY name", (root, dir))}

    def put_files(self, root: str, dir: str, records: Sequence[FileRecord]) -> None:
        if not records:
            return
        # 同一调用内重复的名称也遵循整体替换，保留最后一项。
        rows = list({record.name: record for record in records}.values())
        with self._tx() as conn:
            conn.execute("""INSERT INTO mail_files(root,dir,name,size,mtime_ns,mail_id)
                            SELECT %s,%s,u.name,u.size,u.mtime_ns,u.mail_id
                            FROM UNNEST(%s::text[],%s::bigint[],%s::bigint[],%s::text[])
                                AS u(name,size,mtime_ns,mail_id)
                            ON CONFLICT(root,dir,name) DO UPDATE SET size=EXCLUDED.size,
                                mtime_ns=EXCLUDED.mtime_ns,mail_id=EXCLUDED.mail_id""",
                         (root, dir, [r.name for r in rows], [r.size for r in rows],
                          [r.mtime_ns for r in rows], [r.mail_id for r in rows]))

    def remove_files(self, root: str, dir: str, names: Collection[str]) -> None:
        if names:
            with self._tx() as conn:
                conn.execute("DELETE FROM mail_files WHERE root=%s AND dir=%s AND name=ANY(%s)", (root, dir, list(names)))

    def tree_mail_ids(self, root: str, dir: str, *, batch: int = BULK) -> Iterator[list[str]]:
        if batch < 1:
            raise ValueError("batch 必须为正数")
        predicate, params = self._subtree(root, dir)
        cursor: tuple[str, str] | None = None
        while True:
            condition = " AND (dir,name) > (%s,%s)" if cursor is not None else ""
            with self._tx() as conn:
                rows = conn.execute("SELECT dir,name,mail_id FROM mail_files WHERE " + predicate +
                                    " AND mail_id IS NOT NULL" + condition + " ORDER BY dir,name LIMIT %s",
                                    (*params, *(cursor or ()), batch)).fetchall()
            if not rows:
                return
            cursor = rows[-1][0], rows[-1][1]
            # 事务和实例锁在交给调用方之前已经释放。
            yield [row[2] for row in rows]
            if len(rows) < batch:
                return

    def remove_tree(self, root: str, dir: str) -> int:
        predicate, params = self._subtree(root, dir)
        with self._tx() as conn, conn.transaction():
            conn.execute("DELETE FROM mail_files WHERE " + predicate, params)
            return conn.execute("DELETE FROM mail_dirs WHERE " + predicate, params).rowcount

    def folder_counts(self, *, depth: int) -> dict[str, int]:
        if depth < 1:
            raise ValueError("depth 必须为正数")
        with self._tx() as conn:
            return dict(conn.execute("""SELECT folder,sum(mails) FROM (
                SELECT array_to_string((string_to_array(
                    CASE WHEN dir='' THEN root ELSE root||'/'||dir END,'/'))[1:%s],'/') AS folder,mails
                FROM mail_dirs WHERE mails>0) counts GROUP BY folder ORDER BY folder""", (depth,)).fetchall())

    def stats(self, root: str) -> dict[str, int]:
        with self._tx() as conn:
            dirs, mails = conn.execute("SELECT count(*),COALESCE(sum(mails),0) FROM mail_dirs WHERE root=%s", (root,)).fetchone()
            return {"dirs": dirs, "mails": mails}

    def save_run(self, report: dict[str, Any]) -> None:
        with self._tx() as conn:
            conn.execute("""INSERT INTO mail_runs(scan_id,root,started_at,report) VALUES (%s,%s,%s,%s)
                            ON CONFLICT(scan_id) DO UPDATE SET root=EXCLUDED.root,
                                started_at=EXCLUDED.started_at,report=EXCLUDED.report""",
                         (report["scan_id"], report["root"], report["started_at"], Jsonb(report)))

    def runs(self, root: str, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit 必须为正数")
        with self._tx() as conn:
            return [row[0] for row in conn.execute(
                "SELECT report FROM mail_runs WHERE root=%s AND NOT (report ? 'parent') "
                "ORDER BY started_at DESC,scan_id DESC LIMIT %s", (root, limit))]

    def parts(self, scan_id: str) -> list[dict[str, Any]]:
        """批次 17b（架构方添加）：分片报告，走 008 的表达式索引。"""
        with self._tx() as conn:
            return [row[0] for row in conn.execute(
                "SELECT report FROM mail_runs WHERE report->>'parent' = %s ORDER BY scan_id", (scan_id,))]

    def run(self, scan_id: str) -> dict[str, Any] | None:
        with self._tx() as conn:
            row = conn.execute("SELECT report FROM mail_runs WHERE scan_id=%s", (scan_id,)).fetchone()
            return row[0] if row else None

    @contextmanager
    def _tx(self) -> Iterator[psycopg.Connection]:
        """单语句使用 PG 隐式短事务，避免额外的 BEGIN/COMMIT 往返；多语句删除显式开启事务。"""
        with self._lock:
            conn = self._conn
            if not conn.autocommit:
                conn.autocommit = True
            yield conn

    @staticmethod
    def _subtree(root: str, dir: str) -> tuple[str, tuple[str, ...]]:
        """根分支不带 OR；其他目录按 C 排序的主键区间限定子树。"""
        if not dir:
            return "root=%s", (root,)
        return "root=%s AND (dir=%s OR (dir>=%s AND dir<%s))", (root, dir, dir + "/", dir + "0")
