"""账号与会话：端口与用例（批次 16a）。规格见 docs/spec-28-auth.md 第二节。"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from core.domain.access_policy import Principal
from core.domain.identity import (ALL_GROUP, ALL_GROUP_NAME, Action, Role, Session, User, allowed, locked_until, needs_touch,
                                  normalize_name, normalize_username, password_problem, principal_for, session_valid)


class AlreadyExists(Exception):
    """登录名、组 ID 或组名已被占用。"""


class AuthFailed(Exception):
    """登录失败。消息统一为“登录名或密码错误”，不区分账号不存在、已停用或密码错误。"""

    def __init__(self) -> None:
        super().__init__("登录名或密码错误")


class AccountLocked(Exception):
    """连续失败次数过多，账号暂时锁定。until：解锁时刻。"""

    def __init__(self, until: datetime) -> None:
        super().__init__("尝试次数过多，请稍后再试")
        self.until = until


class Forbidden(Exception):
    """当前账号的角色不允许该操作，或操作会造成不允许的状态（例如停用最后一个管理员）。"""


@dataclass(frozen=True)
class Group:
    group_id: str
    name: str


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    at: datetime
    actor: str | None          # 操作者 user_id；登录失败等没有确定账号时为 None
    action: str                # 例如 "login"、"login_failed"、"user_created"、"acl_folder_set"
    target: str | None         # 被操作的对象（user_id、group_id、文件夹路径、version_id）
    detail: dict[str, Any]


@runtime_checkable
class IdentityRepo(Protocol):
    """账号、用户组、会话与审计的存储。返回值都是副本。不做权限判断（由 AuthService 负责）。"""

    # 账号
    def create_user(self, user: User, password_hash: str | None) -> None:
        """user_id 或 username 已存在 → AlreadyExists。"""

    def user(self, user_id: str) -> User | None: ...

    def user_by_name(self, username: str) -> User | None:
        """按规范登录名查找。"""

    def users(self) -> list[User]:
        """全部账号，按 username 升序。"""

    def save_user(self, user: User) -> None:
        """按 user_id 整体替换（不能改 username）；不存在 → KeyError；username 与已存在的记录不同 → ValueError。"""

    def password_hash(self, user_id: str) -> str | None:
        """不存在的账号或没有本地密码 → None。"""

    def set_password_hash(self, user_id: str, password_hash: str) -> None:
        """不存在 → KeyError。"""

    def failures(self, user_id: str) -> tuple[int, datetime | None]:
        """(连续失败次数, 最近一次失败时刻)；没有记录为 (0, None)。"""

    def record_failure(self, user_id: str, at: datetime) -> int:
        """失败次数加一并记录时刻，返回新的次数。"""

    def reset_failures(self, user_id: str) -> None: ...

    # 用户组（不含内置 ALL_GROUP）
    def create_group(self, group: Group) -> None:
        """group_id 已存在或 name 与已有组相同 → AlreadyExists。"""

    def rename_group(self, group_id: str, name: str) -> None:
        """不存在 → KeyError；name 与其他组相同 → AlreadyExists。"""

    def delete_group(self, group_id: str) -> None:
        """同时删除成员关系；不存在 → KeyError。"""

    def groups(self) -> list[Group]:
        """按 name 升序（Unicode 码点序）。"""

    def members(self, group_id: str) -> list[str]:
        """组内 user_id，升序；组不存在 → KeyError。"""

    def set_members(self, group_id: str, user_ids: Collection[str]) -> None:
        """整体替换组成员。组不存在 → KeyError；任一 user_id 不存在 → KeyError（不修改）。"""

    def user_groups(self, user_id: str) -> frozenset[str]:
        """该账号所在的自建组 ID（不含 ALL_GROUP）。"""

    # 会话
    def create_session(self, session: Session) -> None: ...

    def session(self, token_hash: str) -> Session | None: ...

    def touch_session(self, token_hash: str, at: datetime) -> None:
        """更新 last_seen；不存在则忽略。"""

    def delete_session(self, token_hash: str) -> None:
        """不存在则忽略。"""

    def delete_user_sessions(self, user_id: str, *, keep: str | None = None) -> int:
        """删除该账号的全部会话（keep 给出的 token_hash 除外），返回删除数量。"""

    # 审计
    def append_audit(self, at: datetime, actor: str | None, action: str, target: str | None,
                     detail: dict[str, Any]) -> int:
        """追加一条，返回递增的 seq。"""

    def audit(self, *, limit: int = 100, before: int | None = None, target: str | None = None) -> list[AuditEntry]:
        """最新的在前；before 给出时只取 seq < before；target 给出时只取该对象。limit 取 1–500，否则 ValueError。"""


@runtime_checkable
class PasswordHasher(Protocol):
    def hash(self, password: str) -> str:
        """返回自描述的哈希串（含算法、参数与随机盐），同一密码每次结果不同。"""

    def verify(self, password: str, password_hash: str) -> bool:
        """常数时间比较。哈希串格式无法识别 → False。"""


def token_digest(token: str) -> str:
    """会话令牌的存储形式：sha256 十六进制。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthService:
    """登录、会话与账号管理。每个管理方法的第一个参数 actor 是当前操作者；
    actor 的角色不允许（见 domain.identity.allowed）→ Forbidden。所有成功的写操作和登录尝试都写审计。

    - login(username, password)：
        - 登录名不合规、账号不存在、已停用、没有本地密码或密码错误 → AuthFailed（统一消息）。
          账号不存在时也要用 hasher.verify 比对一个固定哈希，避免用时差异暴露账号是否存在。
        - 先检查锁定：locked_until(失败次数, 最近失败时刻) 晚于当前时刻 → AccountLocked（不比对密码，不再加次数）；
          锁定期过后的第一次尝试照常比对，失败时次数从 1 重新计。
        - 密码错误：record_failure，审计 "login_failed"（actor None，target 为 user_id）。
        - 成功：reset_failures；令牌 = token_factory()；保存 Session(token_digest(令牌), …, now, now)；审计 "login"；
          返回 (令牌, User)。
        - 不存在的登录名也审计 "login_failed"（target None，detail {"username": 规范化前截断到 64 字的原文}）。
    - resolve(token)：令牌对应的会话有效（session_valid）且账号存在、启用 → (User, Principal)，需要时 touch；
      否则 None，并删除失效的会话。
    - logout(token)：删除会话，审计 "logout"；令牌无效时什么都不做。
    - change_password(user, token, old, new)：old 不对 → AuthFailed（计入失败次数）；new 不合规 → ValueError(原因)；
      成功后删除该账号除当前会话外的全部会话，审计 "password_changed"。
    - create_user(actor, username, display_name, role, password) → User（需 MANAGE_USERS）；
      user_id 为 "u" + 12 位随机十六进制；password 不合规 → ValueError；审计 "user_created"（detail 不含密码）。
    - update_user(actor, user_id, *, display_name=None, role=None, active=None) → User（需 MANAGE_USERS）：
      操作后系统中不能没有启用的管理员 → Forbidden；停用时删除该账号的全部会话；审计 "user_updated"（detail 为变更项）。
    - reset_password(actor, user_id, password)（需 MANAGE_USERS）：删除该账号全部会话并清零失败次数，审计 "password_reset"。
    - create_group(actor, name) → Group（需 MANAGE_USERS）：group_id 为 "g" + 12 位随机十六进制；审计 "group_created"。
    - rename_group / set_members（需 MANAGE_USERS）：group_id 为 ALL_GROUP → Forbidden；审计 "group_renamed" / "group_members_set"。
    - delete_group(actor, group_id, *, in_use)（需 MANAGE_USERS）：ALL_GROUP → Forbidden；in_use(group_id) 为真
      （仍被文件夹授权或单文档例外引用）→ ValueError；审计 "group_deleted"。
    - groups() → [Group(ALL_GROUP, ALL_GROUP_NAME)] + repo.groups()。
    - bootstrap_admin(username, display_name, password) → User：系统中已有任何管理员 → Forbidden；
      审计 "admin_bootstrapped"（actor None）。只给命令行工具使用。"""

    def __init__(self, repo: IdentityRepo, hasher: PasswordHasher, *, clock: Callable[[], datetime],
                 token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
                 id_factory: Callable[[], str] = lambda: uuid.uuid4().hex[:12]) -> None:
        self._repo, self._hasher, self._clock = repo, hasher, clock
        self._token_factory, self._id_factory = token_factory, id_factory
        self._dummy = hasher.hash("haina-dummy-password-0")

    @property
    def repo(self) -> IdentityRepo:
        return self._repo

    # —— 登录与会话 ——

    def login(self, username: str, password: str) -> tuple[str, User]:
        now = self._clock()
        try:
            name = normalize_username(username)
        except ValueError:
            name = None
        user = self._repo.user_by_name(name) if name else None
        if user is None:
            self._hasher.verify(password if isinstance(password, str) else "", self._dummy)
            self._repo.append_audit(now, None, "login_failed", None,
                                    {"username": str(username)[:64] if isinstance(username, str) else ""})
            raise AuthFailed()
        failures, last = self._repo.failures(user.user_id)
        until = locked_until(failures, last)
        if until is not None and until > now:
            raise AccountLocked(until)
        if until is not None:
            self._repo.reset_failures(user.user_id)
        stored = self._repo.password_hash(user.user_id)
        ok = stored is not None and isinstance(password, str) and self._hasher.verify(password, stored)
        if stored is None:
            self._hasher.verify(password if isinstance(password, str) else "", self._dummy)
        if not ok or not user.active:
            self._repo.record_failure(user.user_id, now)
            self._repo.append_audit(now, None, "login_failed", user.user_id, {})
            raise AuthFailed()
        self._repo.reset_failures(user.user_id)
        token = self._token_factory()
        self._repo.create_session(Session(token_digest(token), user.user_id, now, now))
        self._repo.append_audit(now, user.user_id, "login", user.user_id, {})
        return token, user

    def resolve(self, token: str | None) -> tuple[User, Principal] | None:
        if not token:
            return None
        digest = token_digest(token)
        session = self._repo.session(digest)
        if session is None:
            return None
        now = self._clock()
        user = self._repo.user(session.user_id)
        if user is None or not user.active or not session_valid(session, now):
            self._repo.delete_session(digest)
            return None
        if needs_touch(session, now):
            self._repo.touch_session(digest, now)
        return user, principal_for(user, self._repo.user_groups(user.user_id))

    def logout(self, token: str | None) -> None:
        if not token:
            return
        digest = token_digest(token)
        session = self._repo.session(digest)
        if session is None:
            return
        self._repo.delete_session(digest)
        self._repo.append_audit(self._clock(), session.user_id, "logout", session.user_id, {})

    def change_password(self, user: User, token: str, old: str, new: str) -> None:
        now = self._clock()
        stored = self._repo.password_hash(user.user_id)
        if stored is None or not self._hasher.verify(old, stored):
            self._repo.record_failure(user.user_id, now)
            self._repo.append_audit(now, user.user_id, "password_change_failed", user.user_id, {})
            raise AuthFailed()
        problem = password_problem(new, username=user.username)
        if problem is not None:
            raise ValueError(problem)
        self._repo.set_password_hash(user.user_id, self._hasher.hash(new))
        self._repo.delete_user_sessions(user.user_id, keep=token_digest(token))
        self._repo.append_audit(now, user.user_id, "password_changed", user.user_id, {})

    # —— 账号管理 ——

    def _require(self, actor: User, action: Action) -> None:
        if not actor.active or not allowed(actor.role, action):
            raise Forbidden("没有执行该操作的权限")

    def _active_admins(self) -> int:
        return sum(1 for user in self._repo.users() if user.active and user.role is Role.ADMIN)

    def create_user(self, actor: User, username: str, display_name: str, role: Role, password: str) -> User:
        self._require(actor, Action.MANAGE_USERS)
        return self._create(actor.user_id, username, display_name, role, password, "user_created")

    def _create(self, actor_id: str | None, username: str, display_name: str, role: Role, password: str,
                action: str) -> User:
        name = normalize_username(username)
        display = normalize_name(display_name, field="显示名")
        role = Role(role)
        problem = password_problem(password, username=name)
        if problem is not None:
            raise ValueError(problem)
        user = User("u" + self._id_factory(), name, display, role, True)
        self._repo.create_user(user, self._hasher.hash(password))
        self._repo.append_audit(self._clock(), actor_id, action, user.user_id,
                                {"username": name, "role": role.value})
        return user

    def update_user(self, actor: User, user_id: str, *, display_name: str | None = None, role: Role | None = None,
                    active: bool | None = None) -> User:
        self._require(actor, Action.MANAGE_USERS)
        current = self._repo.user(user_id)
        if current is None:
            raise KeyError(user_id)
        changes: dict[str, Any] = {}
        updated = current
        if display_name is not None:
            updated = replace(updated, display_name=normalize_name(display_name, field="显示名"))
            changes["display_name"] = updated.display_name
        if role is not None:
            updated = replace(updated, role=Role(role))
            changes["role"] = updated.role.value
        if active is not None:
            updated = replace(updated, active=bool(active))
            changes["active"] = updated.active
        was_admin = current.active and current.role is Role.ADMIN
        is_admin = updated.active and updated.role is Role.ADMIN
        if was_admin and not is_admin and self._active_admins() <= 1:
            raise Forbidden("不能停用或降级最后一个管理员")
        self._repo.save_user(updated)
        if not updated.active:
            self._repo.delete_user_sessions(user_id)
        self._repo.append_audit(self._clock(), actor.user_id, "user_updated", user_id, changes)
        return updated

    def reset_password(self, actor: User, user_id: str, password: str) -> None:
        self._require(actor, Action.MANAGE_USERS)
        user = self._repo.user(user_id)
        if user is None:
            raise KeyError(user_id)
        problem = password_problem(password, username=user.username)
        if problem is not None:
            raise ValueError(problem)
        self._repo.set_password_hash(user_id, self._hasher.hash(password))
        self._repo.reset_failures(user_id)
        self._repo.delete_user_sessions(user_id)
        self._repo.append_audit(self._clock(), actor.user_id, "password_reset", user_id, {})

    def bootstrap_admin(self, username: str, display_name: str, password: str) -> User:
        if any(user.role is Role.ADMIN for user in self._repo.users()):
            raise Forbidden("系统中已有管理员")
        return self._create(None, username, display_name, Role.ADMIN, password, "admin_bootstrapped")

    # —— 用户组 ——

    def groups(self) -> list[Group]:
        return [Group(ALL_GROUP, ALL_GROUP_NAME), *self._repo.groups()]

    def group_ids(self) -> frozenset[str]:
        return frozenset({ALL_GROUP} | {group.group_id for group in self._repo.groups()})

    def create_group(self, actor: User, name: str) -> Group:
        self._require(actor, Action.MANAGE_USERS)
        name = normalize_name(name, field="组名")
        if name == ALL_GROUP_NAME:
            raise AlreadyExists("组名已存在")
        group = Group("g" + self._id_factory(), name)
        self._repo.create_group(group)
        self._repo.append_audit(self._clock(), actor.user_id, "group_created", group.group_id, {"name": name})
        return group

    def _custom(self, group_id: str) -> None:
        if group_id == ALL_GROUP:
            raise Forbidden("“全体员工”组不能修改")

    def rename_group(self, actor: User, group_id: str, name: str) -> None:
        self._require(actor, Action.MANAGE_USERS)
        self._custom(group_id)
        name = normalize_name(name, field="组名")
        if name == ALL_GROUP_NAME:
            raise AlreadyExists("组名已存在")
        self._repo.rename_group(group_id, name)
        self._repo.append_audit(self._clock(), actor.user_id, "group_renamed", group_id, {"name": name})

    def set_members(self, actor: User, group_id: str, user_ids: Sequence[str]) -> None:
        self._require(actor, Action.MANAGE_USERS)
        self._custom(group_id)
        members = sorted(set(user_ids))
        self._repo.set_members(group_id, members)
        self._repo.append_audit(self._clock(), actor.user_id, "group_members_set", group_id, {"members": members})

    def delete_group(self, actor: User, group_id: str, *, in_use: Callable[[str], bool]) -> None:
        self._require(actor, Action.MANAGE_USERS)
        self._custom(group_id)
        self._repo.members(group_id)                     # 不存在 → KeyError
        if in_use(group_id):
            raise ValueError("该组仍被文件夹授权或单文档例外引用，请先移除这些授权")
        self._repo.delete_group(group_id)
        self._repo.append_audit(self._clock(), actor.user_id, "group_deleted", group_id, {})
