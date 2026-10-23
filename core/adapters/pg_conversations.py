"""ConversationRepo 的 PostgreSQL 实现（批次 19）。表结构见 pg_migrations/010_conversations.sql。

- 每个方法一个短事务；实例内用锁串行化连接；连接断开后下次调用重连。
- append 先锁住对话行（SELECT … FOR UPDATE），序号取当前 turns，多进程同时追加也不会冲突。
- 构造时表不存在 → ValueError（提示先迁移）。"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from threading import RLock
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from core.adapters.pg_registry import _connect
from core.domain.conversation import Conversation, ConversationSummary
from core.domain.purge import scrub_turn

_COLUMNS = "conversation_id,owner,title,created_at,updated_at,turns"


class PgConversationRepo:
    def __init__(self, dsn: str, *, schema: str = "public") -> None:
        self._dsn, self._schema = dsn, schema
        self._lock = RLock()
        self._connection = _connect(dsn, schema)
        try:
            with self._connection.transaction():
                self._connection.execute("SELECT 1 FROM conversations LIMIT 0")
        except psycopg.errors.UndefinedTable as exc:
            self._connection.close()
            raise ValueError("数据库尚未迁移到对话记录结构，请先运行迁移") from exc

    @property
    def _conn(self) -> psycopg.Connection:
        if self._connection.closed or self._connection.broken:
            self._connection = _connect(self._dsn, self._schema)
        return self._connection

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create(self, conversation_id: str, owner: str, title: str, at: str) -> ConversationSummary:
        with self._lock, self._conn.transaction():
            try:
                with self._conn.transaction():
                    self._conn.execute(f"INSERT INTO conversations({_COLUMNS}) VALUES (%s,%s,%s,%s,%s,0)",
                                       (conversation_id, owner, title, at, at))
            except psycopg.errors.UniqueViolation as exc:
                raise ValueError("对话已存在") from exc
            return ConversationSummary(conversation_id, owner, title, at, at, 0)

    def get(self, conversation_id: str) -> Conversation | None:
        with self._lock, self._conn.transaction():
            row = self._conn.execute(f"SELECT {_COLUMNS} FROM conversations WHERE conversation_id=%s",
                                     (conversation_id,)).fetchone()
            if row is None:
                return None
            turns = [value[0] for value in self._conn.execute(
                "SELECT data FROM conversation_turns WHERE conversation_id=%s ORDER BY ordinal", (conversation_id,))]
            return Conversation(ConversationSummary(*row), tuple(turns))

    def summaries(self, owner: str, *, limit: int = 50, offset: int = 0) -> list[ConversationSummary]:
        with self._lock, self._conn.transaction():
            return [ConversationSummary(*row) for row in self._conn.execute(
                f"SELECT {_COLUMNS} FROM conversations WHERE owner=%s "
                "ORDER BY updated_at DESC, conversation_id DESC LIMIT %s OFFSET %s", (owner, limit, offset))]

    def count(self, owner: str) -> int:
        with self._lock, self._conn.transaction():
            return self._conn.execute("SELECT count(*) FROM conversations WHERE owner=%s", (owner,)).fetchone()[0]

    def append(self, conversation_id: str, turn: Mapping[str, Any], at: str) -> ConversationSummary:
        with self._lock, self._conn.transaction():
            row = self._conn.execute("SELECT turns FROM conversations WHERE conversation_id=%s FOR UPDATE",
                                     (conversation_id,)).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            self._conn.execute("INSERT INTO conversation_turns(conversation_id,ordinal,data) VALUES (%s,%s,%s)",
                               (conversation_id, row[0], Jsonb(dict(turn))))
            updated = self._conn.execute(
                f"UPDATE conversations SET turns=turns+1, updated_at=%s WHERE conversation_id=%s RETURNING {_COLUMNS}",
                (at, conversation_id)).fetchone()
            return ConversationSummary(*updated)

    def rename(self, conversation_id: str, title: str, at: str) -> ConversationSummary:
        with self._lock, self._conn.transaction():
            row = self._conn.execute(
                f"UPDATE conversations SET title=%s, updated_at=%s WHERE conversation_id=%s RETURNING {_COLUMNS}",
                (title, at, conversation_id)).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            return ConversationSummary(*row)

    def scrub_versions(self, version_ids: Collection[str]) -> int:
        """用 jsonb 包含查询找出资料或引文涉及这些版本的轮次，逐条按 scrub_turn 替换。"""
        count = 0
        with self._lock, self._conn.transaction():
            for version in sorted(set(version_ids)):
                rows = self._conn.execute(
                    "SELECT conversation_id,ordinal,data FROM conversation_turns WHERE data->'sources' @> %s "
                    "OR data->'citations' @> %s FOR UPDATE",
                    (Jsonb([{"version_id": version}]), Jsonb([{"source": {"version_id": version}}]))).fetchall()
                for conversation_id, ordinal, data in rows:
                    new = scrub_turn(data, {version})
                    if new is not None:
                        self._conn.execute("UPDATE conversation_turns SET data=%s WHERE conversation_id=%s AND ordinal=%s",
                                           (Jsonb(new), conversation_id, ordinal))
                        count += 1
        return count

    def delete(self, conversation_id: str) -> bool:
        with self._lock, self._conn.transaction():
            return self._conn.execute("DELETE FROM conversations WHERE conversation_id=%s",
                                      (conversation_id,)).rowcount > 0
