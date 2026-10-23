"""Web 层的身份与权限闸门（批次 16a）。规格见 docs/spec-28-auth.md 第四节。

两种模式：
- 开放模式（OpenGate）：不登录，所有请求视为本机管理员“本机”，能读全部文档；检索使用登记处的固定标签。
  JSON 模式、旧测试与本机单人试用使用。
- 账号模式（AccountGate）：Cookie 会话；角色决定能做哪类事，文件夹授权决定能读哪些文档；管理员没有阅读豁免。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from core.app.identity import AuthService
from core.app.permissions import PermissionService
from core.domain.access_policy import Principal
from core.domain.identity import ALL_GROUP, Action, Role, User, allowed
from core.domain.permissions import FolderScope, Visibility

COOKIE = "haina_session"
CSRF_HEADER = "x-haina-request"          # 账号模式下所有非 GET/HEAD 的 /api 请求必须带 X-Haina-Request: 1
LOCAL_USER = User("local", "local", "本机", Role.ADMIN, True)


@dataclass(frozen=True)
class Access:
    user: User
    principal: Principal
    token: str | None = None

    def can(self, action: Action) -> bool:
        return allowed(self.user.role, action)


class OpenGate:
    """开放模式。"""

    enabled = False

    def __init__(self, legacy_tag: str) -> None:
        self._tag = legacy_tag
        self._access = Access(LOCAL_USER, Principal("local", frozenset({legacy_tag, ALL_GROUP})))

    def resolve(self, token: str | None) -> Access:
        return self._access

    def can_read(self, access: Access, row: Mapping[str, Any]) -> bool:
        return True

    def filter_rows(self, access: Access, rows: Iterable[Any]) -> list[Any]:
        return list(rows)

    def can_upload(self, access: Access, folder: str) -> bool:
        return True

    def search_tags(self, access: Access) -> list[str]:
        return [self._tag]

    def scope(self, access: Access) -> None:
        """开放模式不限范围。"""
        return None

    def mail_scope(self, access: Access) -> None:
        """批次 17a：开放模式下邮件也不限范围。"""
        return None


class AccountGate:
    """账号模式。resolve(token) → Access 或 None（未登录、会话失效、账号停用）。
    search_tags：principal 的全部用户组（排序）。其余判断转给 PermissionService。"""

    enabled = True

    def __init__(self, auth: AuthService, permissions: PermissionService) -> None:
        self.auth, self.permissions = auth, permissions

    def resolve(self, token: str | None) -> Access | None:
        resolved = self.auth.resolve(token)
        if resolved is None:
            return None
        user, principal = resolved
        return Access(user, principal, token)

    def can_read(self, access: Access, row: Mapping[str, Any]) -> bool:
        return self.permissions.can_read(access.principal, row)

    def filter_rows(self, access: Access, rows: Iterable[Any]) -> list[Any]:
        return self.permissions.filter_rows(access.principal, rows)

    def can_upload(self, access: Access, folder: str) -> bool:
        return self.permissions.can_upload(access.principal, folder)

    def search_tags(self, access: Access) -> list[str]:
        return sorted(access.principal.group_ids)

    def scope(self, access: Access) -> Visibility:
        """批次 16b：当前账号的可读范围（PermissionService.visibility），交给查询与登记处分页使用。"""
        return self.permissions.visibility(access.principal)

    def mail_scope(self, access: Access) -> FolderScope:
        """批次 17a：当前账号按文件夹的邮件可读范围（PermissionService.folder_scope）。"""
        return self.permissions.folder_scope(access.principal)


def user_json(user: User) -> dict[str, Any]:
    return {"user_id": user.user_id, "username": user.username, "display_name": user.display_name,
            "role": user.role.value, "active": user.active, "source": user.source}


PUBLIC_PATHS = frozenset({"/api/health"})


_PREFIX_ACTIONS: tuple[tuple[str | None, str, Action], ...] = (
    # (方法或 None 表示任意, 路径前缀, 需要的操作)；按顺序取第一个匹配
    (None, "/api/admin/users", Action.MANAGE_USERS),
    (None, "/api/admin/groups", Action.MANAGE_USERS),
    (None, "/api/admin/audit", Action.MANAGE_USERS),
    (None, "/api/admin/", Action.MANAGE_ACL),
    (None, "/api/scan", Action.SCAN),
    (None, "/api/mail/scan", Action.SCAN),                 # 批次 17a：邮件扫描（检索与阅读为 READ，按文件夹授权过滤）
    (None, "/api/review", Action.REVIEW),
    (None, "/api/events", Action.REVIEW),                  # 知识事件流（运维排查）
    (None, "/api/tasks", Action.REVIEW),                   # 全局任务计数（运维信息，16b-Q3），成员看不到
    (None, "/api/graph", Action.REVIEW),                   # 旧版图谱接口（批次 20 移除），不做按文档过滤
    ("POST", "/api/knowledge/extract", Action.REVIEW),     # 触发抽取、引用分析、冲突判断
    ("POST", "/api/links/", Action.REVIEW),
    (None, "/api/links", Action.KNOWLEDGE),
    (None, "/api/knowledge", Action.KNOWLEDGE),
    (None, "/api/timeline", Action.KNOWLEDGE),
    (None, "/api/entity-graph", Action.KNOWLEDGE),
    (None, "/api/entities", Action.KNOWLEDGE),
    ("POST", "/api/uploads", Action.UPLOAD),
    ("PUT", "/api/uploads", Action.UPLOAD),
    ("DELETE", "/api/uploads", Action.UPLOAD),
)


def action_for(method: str, path: str) -> Action | None:
    """路径需要的操作类别；/api/auth/ 下的接口只要求已登录（返回 None）。
    文档的重试与确认（POST /api/documents/{v}/retry|review），以及撤下、恢复、确认版本（withdraw|restore|keep，批次 20a）需要 REVIEW；上传（POST /api/documents）需要 UPLOAD；
    其余 /api/ 接口需要 READ。"""
    if path.startswith("/api/auth/"):
        return None
    if method == "POST" and path.startswith("/api/documents/") and path.endswith("/purge"):
        return Action.PURGE                                                      # 批次 20b：只有管理员
    if method == "POST" and path.startswith("/api/documents/") and path.rsplit("/", 1)[-1] in (
            "retry", "review", "withdraw", "restore", "keep"):                    # 批次 20a：撤下、恢复、确认版本
        return Action.REVIEW
    if method == "POST" and path == "/api/documents":
        return Action.UPLOAD
    for verb, prefix, action in _PREFIX_ACTIONS:
        if (verb is None or verb == method) and (path == prefix.rstrip("/") or path.startswith(prefix)):
            return action
    return Action.READ


class FilteringIndexer:
    """包装 Indexer：search 的结果只保留 readable(version_id) 为真的命中（其余方法原样转发）。
    检索引擎按标签前置过滤；这里再按最新授权过滤一遍，标签同步完成之前也不会返回越权内容。"""

    def __init__(self, inner: Any, readable: Any) -> None:
        self._inner, self._readable = inner, readable

    def search(self, query: str, *, group_tags: Any, top_k: int) -> list[Any]:
        return [hit for hit in self._inner.search(query, group_tags=group_tags, top_k=top_k)
                if self._readable(hit.version_id)]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
