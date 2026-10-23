"""登录与管理接口（批次 16a）。规格见 docs/spec-28-auth.md 第四节。"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool, StrictStr
from starlette.exceptions import HTTPException

from apps.web.auth import COOKIE, Access, AccountGate, OpenGate, user_json
from core.app.identity import AccountLocked, AlreadyExists, AuthFailed, Forbidden
from core.app.registry import DocumentRegistry
from core.domain.access_policy import effective_groups
from core.domain.access_policy import Document as AclDocument
from core.domain.identity import ABSOLUTE_TIMEOUT, Action, Role
from core.domain.permissions import folder_snapshot


class _Login(BaseModel):
    username: StrictStr = Field(min_length=1, max_length=256)
    password: StrictStr = Field(min_length=1, max_length=256)


class _PasswordChange(BaseModel):
    old: StrictStr = Field(min_length=1, max_length=256)
    new: StrictStr = Field(min_length=1, max_length=256)


class _NewUser(BaseModel):
    username: StrictStr = Field(min_length=1, max_length=64)
    display_name: StrictStr = Field(min_length=1, max_length=64)
    role: StrictStr
    password: StrictStr = Field(min_length=1, max_length=256)


class _UserUpdate(BaseModel):
    display_name: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    role: StrictStr | None = None
    active: StrictBool | None = None


class _Password(BaseModel):
    password: StrictStr = Field(min_length=1, max_length=256)


class _GroupName(BaseModel):
    name: StrictStr = Field(min_length=1, max_length=64)


class _Members(BaseModel):
    user_ids: list[StrictStr] = Field(max_length=10_000)


class _FolderAcl(BaseModel):
    path: StrictStr = Field(max_length=8192)
    groups: list[StrictStr] | None = Field(default=None, max_length=200)


class _DocumentAcl(BaseModel):
    groups: list[StrictStr] | None = Field(default=None, max_length=200)
    reason: StrictStr = Field(min_length=1, max_length=500)


def _role(value: str | None) -> Role | None:
    if value is None:
        return None
    try:
        return Role(value)
    except ValueError:
        raise ValueError("角色只能是 admin、reviewer、member") from None


def register(app: FastAPI, gate: OpenGate | AccountGate, store: DocumentRegistry, *,
             response: Callable[..., JSONResponse], current: Callable[[Request], Access], acl_sync: Any,
             secure_cookie: bool, mail_folders: Callable[[], dict[str, int]] | None = None) -> None:
    """注册 /api/auth/* 与 /api/admin/*。开放模式下只有 GET /api/auth/me，其余返回 404。
    mail_folders（批次 17a）：邮件文件夹的邮件数（MailCatalog.folder_counts），未启用邮件时为 None。"""

    @app.get("/api/auth/me")
    def me(access: Access = Depends(current)) -> JSONResponse:
        """{"auth_enabled", "user", "actions": 允许的操作（升序）, "groups": 所在的用户组 [{group_id, name}],
        "features": {"mail": 是否启用邮件}（批次 17a）}。"""
        if gate.enabled:
            names = {group.group_id: group.name for group in gate.auth.groups()}
            groups = [{"group_id": g, "name": names.get(g, g)} for g in sorted(access.principal.group_ids)]
        else:
            groups = []
        return response({"auth_enabled": gate.enabled, "user": user_json(access.user),
                         "actions": sorted(action.value for action in Action if access.can(action)), "groups": groups,
                         "features": {"mail": mail_folders is not None}})

    if not gate.enabled:
        return
    auth, permissions = gate.auth, gate.permissions

    def set_cookie(result: JSONResponse, token: str) -> JSONResponse:
        result.set_cookie(COOKIE, token, max_age=int(ABSOLUTE_TIMEOUT.total_seconds()), httponly=True,
                          samesite="strict", secure=secure_cookie, path="/")
        return result

    @app.post("/api/auth/login")
    def login(body: _Login) -> JSONResponse:
        """成功 → {"user"} 并设置会话 Cookie（HttpOnly、SameSite=Strict）；失败 → 401；锁定 → 429 {"error","until"}。"""
        try:
            token, user = auth.login(body.username, body.password)
        except AccountLocked as exc:
            return response({"error": str(exc), "until": exc.until.isoformat()}, 429)
        except AuthFailed as exc:
            return response({"error": str(exc)}, 401)
        return set_cookie(response({"user": user_json(user)}), token)

    @app.post("/api/auth/logout")
    def logout(access: Access = Depends(current)) -> JSONResponse:
        auth.logout(access.token)
        result = response({"ok": True})
        result.delete_cookie(COOKIE, path="/")
        return result

    @app.post("/api/auth/password")
    def change_password(body: _PasswordChange, access: Access = Depends(current)) -> JSONResponse:
        """旧密码不对 → 401；新密码不合规 → 400。成功后其他设备上的会话失效。"""
        try:
            auth.change_password(access.user, access.token or "", body.old, body.new)
        except AuthFailed:
            return response({"error": "原密码不正确"}, 401)
        except ValueError as exc:
            return response({"error": str(exc)}, 400)
        return response({"ok": True})

    # —— 账号与用户组（MANAGE_USERS，中间件已检查角色）——

    def users_json() -> list[dict[str, Any]]:
        repo = auth.repo
        return [{**user_json(user), "groups": sorted(repo.user_groups(user.user_id))} for user in repo.users()]

    def guarded(call: Callable[[], Any]) -> Any:
        try:
            return call()
        except Forbidden as exc:
            return response({"error": str(exc)}, 403)
        except AlreadyExists as exc:
            return response({"error": str(exc) or "已存在"}, 409)
        except KeyError:
            raise HTTPException(404)
        except ValueError as exc:
            return response({"error": str(exc)}, 400)

    @app.get("/api/admin/users")
    def list_users() -> JSONResponse:
        return response({"users": users_json()})

    @app.post("/api/admin/users")
    def create_user(body: _NewUser, access: Access = Depends(current)) -> JSONResponse:
        def call() -> JSONResponse:
            user = auth.create_user(access.user, body.username, body.display_name, _role(body.role), body.password)
            return response({"user": user_json(user)}, 201)
        return guarded(call)

    @app.patch("/api/admin/users/{user_id}")
    def update_user(user_id: str, body: _UserUpdate, access: Access = Depends(current)) -> JSONResponse:
        def call() -> JSONResponse:
            user = auth.update_user(access.user, user_id, display_name=body.display_name, role=_role(body.role),
                                    active=body.active)
            return response({"user": user_json(user)})
        return guarded(call)

    @app.post("/api/admin/users/{user_id}/password")
    def reset_password(user_id: str, body: _Password, access: Access = Depends(current)) -> JSONResponse:
        return guarded(lambda: (auth.reset_password(access.user, user_id, body.password), response({"ok": True}))[1])

    def groups_json() -> list[dict[str, Any]]:
        repo = auth.repo
        rows = []
        for group in auth.groups():
            builtin = group.group_id == "all"
            rows.append({"group_id": group.group_id, "name": group.name, "builtin": builtin,
                         "members": None if builtin else repo.members(group.group_id),
                         "referenced": permissions.repo.references(group.group_id)})
        return rows

    @app.get("/api/admin/groups")
    def list_groups() -> JSONResponse:
        """内置组在最前，members 为 null（隐含全部启用的账号）。"""
        return response({"groups": groups_json()})

    @app.post("/api/admin/groups")
    def create_group(body: _GroupName, access: Access = Depends(current)) -> JSONResponse:
        def call() -> JSONResponse:
            group = auth.create_group(access.user, body.name)
            return response({"group": {"group_id": group.group_id, "name": group.name}}, 201)
        return guarded(call)

    @app.patch("/api/admin/groups/{group_id}")
    def rename_group(group_id: str, body: _GroupName, access: Access = Depends(current)) -> JSONResponse:
        return guarded(lambda: (auth.rename_group(access.user, group_id, body.name), response({"ok": True}))[1])

    @app.put("/api/admin/groups/{group_id}/members")
    def set_members(group_id: str, body: _Members, access: Access = Depends(current)) -> JSONResponse:
        return guarded(lambda: (auth.set_members(access.user, group_id, body.user_ids), response({"ok": True}))[1])

    @app.delete("/api/admin/groups/{group_id}")
    def delete_group(group_id: str, access: Access = Depends(current)) -> JSONResponse:
        """仍被授权引用 → 400；内置组 → 403。"""
        return guarded(lambda: (auth.delete_group(access.user, group_id, in_use=permissions.repo.references),
                                response({"ok": True}))[1])

    @app.get("/api/admin/audit")
    def audit(limit: int = 100, before: int | None = None, target: str | None = None) -> JSONResponse:
        """账号审计，最新的在前：{"entries": [{"seq","at","actor","action","target","detail"}]}。limit 取 1–500。"""
        try:
            rows = auth.repo.audit(limit=limit, before=before, target=target)
        except ValueError as exc:
            return response({"error": str(exc)}, 400)
        return response({"entries": [{"seq": e.seq, "at": e.at.isoformat(), "actor": e.actor, "action": e.action,
                                      "target": e.target, "detail": e.detail} for e in rows]})

    # —— 文件夹授权（MANAGE_ACL）——

    @app.get("/api/admin/folders")
    def folders() -> JSONResponse:
        """文件夹树（含根 ""）：[{"path","name","parent","explicit": 组列表或 null,"effective": 组列表,
        "documents": 直接位于该文件夹的文档数, "overrides": 其中有单文档例外的数量,
        "mails": 邮件数（批次 17a；第 FOLDER_DEPTH 层的文件夹含其全部子孙中的邮件，更浅的只计直接包含的）}]，按 path 升序。
        邮件文件夹只列出前 FOLDER_DEPTH 层（含根名称），与文档文件夹合并为同一棵树。
        管理员能看到文件夹名称与数量，但看不到文档内容（没有阅读豁免）。"""
        snap = permissions.snapshot()
        counts = store.folder_counts()                      # 批次 16b：不再取出全部记录
        overrides = Counter(store.folders_of(snap.doc_override).values())
        mails = mail_folders() if mail_folders is not None else {}
        if mails:
            snap = folder_snapshot([*snap.folders, *mails], snap.folder_acl, snap.doc_override)
        rows = []
        for path in sorted(snap.folders):
            groups = effective_groups(AclDocument("\0folder", path), snap)
            explicit = snap.folder_acl.get(path)
            rows.append({"path": path, "name": path.rsplit("/", 1)[-1], "parent": snap.folders[path].parent_id,
                         "explicit": None if explicit is None else sorted(explicit), "effective": sorted(groups),
                         "documents": counts.get(path, 0), "overrides": overrides.get(path, 0),
                         "mails": mails.get(path, 0)})
        return response({"folders": rows, "revision": permissions.repo.revision()})

    @app.put("/api/admin/folders/acl")
    def set_folder_acl(body: _FolderAcl, access: Access = Depends(current)) -> JSONResponse:
        """{"path", "groups": 组 ID 列表（空列表 = 明确拒绝所有人）或 null（改为继承）} →
        {"affected": 受影响的已发布文档数, "revision"}。组不存在、路径非法 → 400。"""
        def call() -> JSONResponse:
            affected = permissions.set_folder_acl(access.user, body.path,
                                                  None if body.groups is None else frozenset(body.groups))
            return response({"affected": len(affected), "revision": permissions.repo.revision()})
        return guarded(call)

    @app.get("/api/admin/documents/{version_id}/acl")
    def document_acl(version_id: str) -> JSONResponse:
        """{"version_id","title","folder","override": 组列表或 null,"effective": 组列表}；文档不存在 → 404。
        管理员可以看到标题与文件夹以便设置例外。"""
        detail = store.detail(version_id)
        if detail is None:
            raise HTTPException(404)
        record = detail["document"]
        snap = permissions.snapshot()
        override = snap.doc_override.get(version_id)
        groups = effective_groups(AclDocument(version_id, record.get("folder", "")), snap)
        return response({"version_id": version_id, "title": record["title"], "folder": record.get("folder", ""),
                         "override": None if override is None else sorted(override), "effective": sorted(groups)})

    @app.put("/api/admin/documents/{version_id}/acl")
    def set_document_acl(version_id: str, body: _DocumentAcl, access: Access = Depends(current)) -> JSONResponse:
        """{"groups": 列表或 null, "reason"} → {"affected","revision"}。"""
        def call() -> JSONResponse:
            affected = permissions.set_override(access.user, version_id,
                                                None if body.groups is None else frozenset(body.groups), body.reason)
            return response({"affected": len(affected), "revision": permissions.repo.revision()})
        return guarded(call)

    @app.get("/api/admin/acl/history")
    def acl_history(target: str | None = None, limit: int = 100) -> JSONResponse:
        try:
            return response({"entries": permissions.repo.history(target=target, limit=limit)})
        except ValueError as exc:
            return response({"error": str(exc)}, 400)

    @app.get("/api/admin/sync")
    def sync_status() -> JSONResponse:
        """检索标签同步：{"stale","queued","running","failed"}。"""
        return response(acl_sync.status())

    @app.post("/api/admin/sync")
    def sync_now() -> JSONResponse:
        task_id = acl_sync.enqueue(permissions.repo.revision())
        return response({"task_id": task_id}, 202)
