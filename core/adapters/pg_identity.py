"""IdentityRepo 与 PermissionRepo 的 PostgreSQL 实现（批次 16a）。表结构见 pg_migrations/005_identity.sql，
语义见 core/app/identity.py、core/app/permissions.py 与契约测试。

- 每个方法一个短事务；实例内用锁串行化连接；连接断开后下次调用重连。
- 构造时表不存在 → ValueError（提示先迁移）。
- 唯一约束冲突（登录名、组 ID、组名）→ AlreadyExists；外键冲突（成员不存在）→ KeyError，且不做任何修改。
- 授权修订号：每次 set_folder_acl / set_override 在同一事务里 UPDATE acl_revision SET revision = revision + 1 RETURNING。
"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from core.adapters.pg_registry import _connect
from core.app.identity import AlreadyExists, AuditEntry, Group
from core.domain.identity import Role, Session, User

_USER = "user_id, username, display_name, role, active, source"


def _user(row: tuple[Any, ...]) -> User:
    return User(row[0], row[1], row[2], Role(row[3]), row[4], row[5])


class _Pg:
    _TABLE = ""

    def __init__(self, dsn: str, *, schema: str = "public") -> None:
        self._dsn, self._schema = dsn, schema
        self._lock = RLock()
        self._connection = _connect(dsn, schema)
        try:
            with self._connection.transaction():
                self._connection.execute(f"SELECT 1 FROM {self._TABLE} LIMIT 0")
        except psycopg.errors.UndefinedTable as exc:
            self._connection.close()
            raise ValueError("数据库尚未迁移到账号与授权结构，请先运行迁移") from exc

    @property
    def _conn(self) -> psycopg.Connection:
        if self._connection.closed or self._connection.broken:
            self._connection = _connect(self._dsn, self._schema)
        return self._connection

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class PgIdentityRepo(_Pg):
    _TABLE = "users"

    # —— 账号 ——

    def create_user(self, user: User, password_hash: str | None) -> None:
        try:
            with self._lock, self._conn.transaction():
                self._conn.execute(
                    "INSERT INTO users(user_id,username,display_name,role,active,source,password_hash) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (user.user_id, user.username, user.display_name, user.role.value, user.active, user.source, password_hash))
        except psycopg.errors.UniqueViolation as exc:
            raise AlreadyExists("账号 ID 或登录名已存在") from exc

    def user(self, user_id: str) -> User | None:
        with self._lock, self._conn.transaction():
            row = self._conn.execute(f"SELECT {_USER} FROM users WHERE user_id=%s", (user_id,)).fetchone()
            return None if row is None else _user(row)

    def user_by_name(self, username: str) -> User | None:
        with self._lock, self._conn.transaction():
            row = self._conn.execute(f"SELECT {_USER} FROM users WHERE username=%s", (username,)).fetchone()
            return None if row is None else _user(row)

    def users(self) -> list[User]:
        with self._lock, self._conn.transaction():
            return [_user(row) for row in self._conn.execute(f"SELECT {_USER} FROM users ORDER BY username").fetchall()]

    def save_user(self, user: User) -> None:
        with self._lock, self._conn.transaction():
            row = self._conn.execute("SELECT username FROM users WHERE user_id=%s FOR UPDATE", (user.user_id,)).fetchone()
            if row is None:
                raise KeyError(user.user_id)
            if row[0] != user.username:
                raise ValueError("不能修改登录名")
            self._conn.execute("UPDATE users SET display_name=%s,role=%s,active=%s,source=%s WHERE user_id=%s",
                               (user.display_name, user.role.value, user.active, user.source, user.user_id))

    def password_hash(self, user_id: str) -> str | None:
        with self._lock, self._conn.transaction():
            row = self._conn.execute("SELECT password_hash FROM users WHERE user_id=%s", (user_id,)).fetchone()
            return None if row is None else row[0]

    def set_password_hash(self, user_id: str, password_hash: str) -> None:
        with self._lock, self._conn.transaction():
            if not self._conn.execute("UPDATE users SET password_hash=%s WHERE user_id=%s",
                                      (password_hash, user_id)).rowcount:
                raise KeyError(user_id)

    def failures(self, user_id: str) -> tuple[int, datetime | None]:
        with self._lock, self._conn.transaction():
            row = self._conn.execute("SELECT failures,last_failure FROM users WHERE user_id=%s", (user_id,)).fetchone()
            return (0, None) if row is None else (row[0], row[1])

    def record_failure(self, user_id: str, at: datetime) -> int:
        with self._lock, self._conn.transaction():
            row = self._conn.execute("UPDATE users SET failures=failures+1,last_failure=%s WHERE user_id=%s RETURNING failures",
                                     (at, user_id)).fetchone()
            if row is None:
                raise KeyError(user_id)
            return row[0]

    def reset_failures(self, user_id: str) -> None:
        with self._lock, self._conn.transaction():
            self._conn.execute("UPDATE users SET failures=0,last_failure=NULL WHERE user_id=%s", (user_id,))

    # —— 用户组 ——

    def create_group(self, group: Group) -> None:
        try:
            with self._lock, self._conn.transaction():
                self._conn.execute("INSERT INTO user_groups(group_id,name) VALUES (%s,%s)", (group.group_id, group.name))
        except psycopg.errors.UniqueViolation as exc:
            raise AlreadyExists("用户组 ID 或名称已存在") from exc

    def rename_group(self, group_id: str, name: str) -> None:
        try:
            with self._lock, self._conn.transaction():
                if not self._conn.execute("UPDATE user_groups SET name=%s WHERE group_id=%s", (name, group_id)).rowcount:
                    raise KeyError(group_id)
        except psycopg.errors.UniqueViolation as exc:
            raise AlreadyExists("用户组名称已存在") from exc

    def delete_group(self, group_id: str) -> None:
        with self._lock, self._conn.transaction():
            if not self._conn.execute("DELETE FROM user_groups WHERE group_id=%s", (group_id,)).rowcount:
                raise KeyError(group_id)

    def groups(self) -> list[Group]:
        with self._lock, self._conn.transaction():
            return [Group(*row) for row in self._conn.execute("SELECT group_id,name FROM user_groups ORDER BY name").fetchall()]

    def members(self, group_id: str) -> list[str]:
        with self._lock, self._conn.transaction():
            if self._conn.execute("SELECT 1 FROM user_groups WHERE group_id=%s FOR SHARE", (group_id,)).fetchone() is None:
                raise KeyError(group_id)
            return [row[0] for row in self._conn.execute("SELECT user_id FROM group_members WHERE group_id=%s ORDER BY user_id",
                                                        (group_id,)).fetchall()]

    def set_members(self, group_id: str, user_ids: Collection[str]) -> None:
        try:
            with self._lock, self._conn.transaction():
                if self._conn.execute("SELECT 1 FROM user_groups WHERE group_id=%s FOR UPDATE", (group_id,)).fetchone() is None:
                    raise KeyError(group_id)
                self._conn.execute("DELETE FROM group_members WHERE group_id=%s", (group_id,))
                with self._conn.cursor() as cursor:
                    cursor.executemany("INSERT INTO group_members(group_id,user_id) VALUES (%s,%s)",
                                       [(group_id, user_id) for user_id in sorted(set(user_ids))])
        except psycopg.errors.ForeignKeyViolation as exc:
            raise KeyError("成员账号不存在") from exc

    def user_groups(self, user_id: str) -> frozenset[str]:
        with self._lock, self._conn.transaction():
            return frozenset(row[0] for row in self._conn.execute("SELECT group_id FROM group_members WHERE user_id=%s",
                                                                 (user_id,)).fetchall())

    # —— 会话 ——

    def create_session(self, session: Session) -> None:
        try:
            with self._lock, self._conn.transaction():
                self._conn.execute("INSERT INTO sessions(token_hash,user_id,created_at,last_seen) VALUES (%s,%s,%s,%s) "
                                   "ON CONFLICT (token_hash) DO UPDATE SET user_id=EXCLUDED.user_id,"
                                   "created_at=EXCLUDED.created_at,last_seen=EXCLUDED.last_seen",
                                   (session.token_hash, session.user_id, session.created_at, session.last_seen))
        except psycopg.errors.ForeignKeyViolation as exc:
            raise KeyError(session.user_id) from exc

    def session(self, token_hash: str) -> Session | None:
        with self._lock, self._conn.transaction():
            row = self._conn.execute("SELECT token_hash,user_id,created_at,last_seen FROM sessions WHERE token_hash=%s",
                                      (token_hash,)).fetchone()
            return None if row is None else Session(*row)

    def touch_session(self, token_hash: str, at: datetime) -> None:
        with self._lock, self._conn.transaction():
            self._conn.execute("UPDATE sessions SET last_seen=%s WHERE token_hash=%s", (at, token_hash))

    def delete_session(self, token_hash: str) -> None:
        with self._lock, self._conn.transaction():
            self._conn.execute("DELETE FROM sessions WHERE token_hash=%s", (token_hash,))

    def delete_user_sessions(self, user_id: str, *, keep: str | None = None) -> int:
        with self._lock, self._conn.transaction():
            if keep is None:
                return self._conn.execute("DELETE FROM sessions WHERE user_id=%s", (user_id,)).rowcount
            return self._conn.execute("DELETE FROM sessions WHERE user_id=%s AND token_hash<>%s", (user_id, keep)).rowcount

    # —— 审计 ——

    def append_audit(self, at: datetime, actor: str | None, action: str, target: str | None,
                     detail: dict[str, Any]) -> int:
        with self._lock, self._conn.transaction():
            return self._conn.execute("INSERT INTO auth_audit(at,actor,action,target,detail) VALUES (%s,%s,%s,%s,%s) RETURNING seq",
                                      (at, actor, action, target, Jsonb(detail))).fetchone()[0]

    def audit(self, *, limit: int = 100, before: int | None = None, target: str | None = None) -> list[AuditEntry]:
        if not 1 <= limit <= 500:
            raise ValueError("limit 取 1–500")
        conditions: list[str] = []
        params: list[Any] = []
        if before is not None:
            conditions.append("seq<%s")
            params.append(before)
        if target is not None:
            conditions.append("target=%s")
            params.append(target)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._lock, self._conn.transaction():
            rows = self._conn.execute("SELECT seq,at,actor,action,target,detail FROM auth_audit" + where +
                                      " ORDER BY seq DESC LIMIT %s", (*params, limit)).fetchall()
            return [AuditEntry(*row) for row in rows]


class PgPermissionRepo(_Pg):
    _TABLE = "folder_acl"

    def revision(self) -> int:
        with self._lock, self._conn.transaction():
            return self._conn.execute("SELECT revision FROM acl_revision WHERE id=1").fetchone()[0]

    def folder_acl(self) -> dict[str, frozenset[str]]:
        with self._lock, self._conn.transaction():
            return {path: frozenset(groups) for path, groups in self._conn.execute("SELECT path,groups FROM folder_acl").fetchall()}

    def _change(self, table: str, key_column: str, key: str, groups: frozenset[str] | None, *, kind: str, actor: str,
                reason: str | None, at: datetime) -> int:
        values = None if groups is None else sorted(groups)
        with self._lock, self._conn.transaction():
            # 先锁定唯一修订行，使不同实例的授权、审计与提交顺序一致。
            revision = self._conn.execute("UPDATE acl_revision SET revision=revision+1 WHERE id=1 RETURNING revision").fetchone()[0]
            if groups is None:
                query = psycopg.sql.SQL("DELETE FROM {} WHERE {}=%s").format(
                    psycopg.sql.Identifier(table), psycopg.sql.Identifier(key_column))
                self._conn.execute(query, (key,))
            else:
                query = psycopg.sql.SQL("INSERT INTO {} ({},groups) VALUES (%s,%s) "
                                       "ON CONFLICT ({}) DO UPDATE SET groups=EXCLUDED.groups").format(
                    psycopg.sql.Identifier(table), psycopg.sql.Identifier(key_column), psycopg.sql.Identifier(key_column))
                self._conn.execute(query, (key, values))
            self._conn.execute("INSERT INTO acl_audit(at,actor,kind,target,groups,reason) VALUES (%s,%s,%s,%s,%s,%s)",
                               (at, actor, kind, key, values, reason))
            return revision

    def set_folder_acl(self, path: str, groups: frozenset[str] | None, *, actor: str, at: datetime) -> int:
        return self._change("folder_acl", "path", path, groups, kind="folder", actor=actor, reason=None, at=at)

    def overrides(self) -> dict[str, frozenset[str]]:
        with self._lock, self._conn.transaction():
            return {version: frozenset(groups) for version, groups in self._conn.execute("SELECT version_id,groups FROM document_acl").fetchall()}

    def set_override(self, version_id: str, groups: frozenset[str] | None, *, actor: str, reason: str,
                     at: datetime) -> int:
        return self._change("document_acl", "version_id", version_id, groups, kind="document", actor=actor, reason=reason, at=at)

    def history(self, *, target: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit 取 1–500")
        where = " WHERE target=%s" if target is not None else ""
        params = (limit,) if target is None else (target, limit)
        with self._lock, self._conn.transaction():
            rows = self._conn.execute("SELECT seq,at,actor,kind,target,groups,reason FROM acl_audit" + where +
                                      " ORDER BY seq DESC LIMIT %s", params).fetchall()
            return [{"seq": seq, "at": at.astimezone(timezone(timedelta(hours=8))).isoformat(), "actor": actor,
                     "kind": kind, "target": target, "groups": groups, "reason": reason}
                    for seq, at, actor, kind, target, groups, reason in rows]

    def references(self, group_id: str) -> bool:
        with self._lock, self._conn.transaction():
            return self._conn.execute("SELECT EXISTS (SELECT 1 FROM folder_acl WHERE %s=ANY(groups) "
                                      "UNION ALL SELECT 1 FROM document_acl WHERE %s=ANY(groups))",
                                      (group_id, group_id)).fetchone()[0]

    def indexed_tags(self) -> dict[str, tuple[str, ...]]:
        with self._lock, self._conn.transaction():
            return {version: tuple(tags) for version, tags in self._conn.execute("SELECT version_id,tags FROM index_tags").fetchall()}

    def record_indexed(self, tags: Mapping[str, Sequence[str]]) -> None:
        if not tags:
            return
        with self._lock, self._conn.transaction(), self._conn.cursor() as cursor:
            cursor.executemany("INSERT INTO index_tags(version_id,tags) VALUES (%s,%s) "
                               "ON CONFLICT (version_id) DO UPDATE SET tags=EXCLUDED.tags",
                               [(version, sorted(values)) for version, values in tags.items()])
