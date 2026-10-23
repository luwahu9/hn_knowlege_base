"""账号、角色、用户组与会话的纯规则（批次 16a）。规格见 docs/spec-28-auth.md。不读时钟：时间由调用方传入。"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from core.domain.access_policy import Principal

ALL_GROUP = "all"                  # 内置“全体员工”组：每个启用的账号都隐含属于它，不能删除、不能手工增减成员
ALL_GROUP_NAME = "全体员工"

USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{1,63}")
GROUP_ID = re.compile(r"g[0-9a-z]{4,31}")   # 自建组的 ID：小写字母开头 g，后接 4–31 位小写字母或数字（由系统生成）
PASSWORD_MIN = 10
PASSWORD_MAX = 256
NAME_MAX = 64

IDLE_TIMEOUT = timedelta(hours=8)          # 会话闲置超过这么久失效
ABSOLUTE_TIMEOUT = timedelta(days=7)       # 会话创建超过这么久失效
TOUCH_INTERVAL = timedelta(minutes=5)      # 至少隔这么久才更新一次 last_seen，减少写入

LOCK_THRESHOLD = 5                          # 连续失败这么多次后锁定
LOCK_DURATION = timedelta(minutes=15)


class Role(str, Enum):
    ADMIN = "admin"            # 管理账号、用户组、文件夹授权、目录扫描；没有阅读豁免
    REVIEWER = "reviewer"      # 审核中心、抽取与连线作业的触发
    MEMBER = "member"          # 检索、问答、文档库、上传


class Action(str, Enum):
    MANAGE_USERS = "manage_users"      # 账号、用户组
    MANAGE_ACL = "manage_acl"          # 文件夹授权、单文档例外、审计
    SCAN = "scan"                      # 服务器目录扫描
    REVIEW = "review"                  # 审核中心、文档重试/确认、实体与事件审核决定
    KNOWLEDGE = "knowledge"            # 知识图谱、时间轴、实体、关联的查看（16b 起按可读文档过滤，成员也可用）
    UPLOAD = "upload"                  # 上传与文件夹上传
    READ = "read"                      # 检索、问答、文档库（结果仍按文件夹授权过滤）
    PURGE = "purge"                    # 批次 20b：彻底删除文档（只有管理员；仍须能读到该文档）


_ALLOWED = {
    Role.ADMIN: frozenset(Action),
    Role.REVIEWER: frozenset({Action.REVIEW, Action.KNOWLEDGE, Action.UPLOAD, Action.READ}),
    Role.MEMBER: frozenset({Action.KNOWLEDGE, Action.UPLOAD, Action.READ}),   # 批次 16b：汇总视图按可读文档过滤后开放
}


def allowed(role: Role, action: Action) -> bool:
    """角色能否执行某类操作。只管“能不能做这类事”；能读到哪些文档另由文件夹授权决定（管理员也不例外）。"""
    return action in _ALLOWED[role]


@dataclass(frozen=True)
class User:
    user_id: str               # 系统生成的不透明 ID，不复用
    username: str              # 登录名（规范形式，见 normalize_username）
    display_name: str
    role: Role
    active: bool
    source: str = "local"      # 认证来源："local"；以后接 LDAP/AD 时为 "ldap"


def normalize_username(raw: str) -> str:
    """登录名规范形式：NFKC、去首尾空白、转小写；必须匹配 USERNAME（2–64 位，小写字母或数字开头，其余可含 . _ -），
    否则 ValueError。"""
    if not isinstance(raw, str):
        raise ValueError("登录名无效")
    value = unicodedata.normalize("NFKC", raw).strip().lower()
    if USERNAME.fullmatch(value) is None:
        raise ValueError("登录名只能包含小写字母、数字和 . _ -，长度 2–64，以字母或数字开头")
    return value


def normalize_name(raw: str, *, field: str = "名称") -> str:
    """显示名、组名：NFC、去首尾空白、内部连续空白压成一个空格；为空、超过 NAME_MAX 字或含控制字符 → ValueError。"""
    if not isinstance(raw, str) or any(unicodedata.category(char) == "Cc" for char in raw):
        raise ValueError(f"{field}含有不允许的字符")
    value = " ".join(unicodedata.normalize("NFC", raw).split())
    if not value or len(value) > NAME_MAX:
        raise ValueError(f"{field}不能为空，且不超过 {NAME_MAX} 字")
    return value


def password_problem(password: str, *, username: str) -> str | None:
    """密码规则，返回中文原因或 None：长度 PASSWORD_MIN–PASSWORD_MAX；不能与登录名相同（忽略大小写）；
    不能全是同一个字符；至少包含字母与数字两类。"""
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        return f"密码长度须为 {PASSWORD_MIN}–{PASSWORD_MAX} 位"
    if password.lower() == username.lower():
        return "密码不能与登录名相同"
    if len(set(password)) == 1:
        return "密码不能全是同一个字符"
    if not (any(char.isalpha() for char in password) and any(char.isdigit() for char in password)):
        return "密码须同时包含字母和数字"
    return None


def principal_for(user: User, groups: frozenset[str]) -> Principal:
    """用于权限计算的身份：用户所在的组再加上 ALL_GROUP。停用的账号不应走到这里（调用方先检查）→ ValueError。"""
    if not user.active:
        raise ValueError("账号已停用")
    return Principal(user.user_id, frozenset(groups) | {ALL_GROUP})


@dataclass(frozen=True)
class Session:
    token_hash: str            # 会话令牌的 sha256 十六进制；令牌原文只在登录响应的 Cookie 中出现
    user_id: str
    created_at: datetime
    last_seen: datetime


def session_valid(session: Session, now: datetime) -> bool:
    """会话未超过闲置时限（now - last_seen < IDLE_TIMEOUT）且未超过绝对时限（now - created_at < ABSOLUTE_TIMEOUT）。"""
    return now - session.last_seen < IDLE_TIMEOUT and now - session.created_at < ABSOLUTE_TIMEOUT


def needs_touch(session: Session, now: datetime) -> bool:
    """距上次记录活动已不少于 TOUCH_INTERVAL。"""
    return now - session.last_seen >= TOUCH_INTERVAL


def locked_until(failures: int, last_failure: datetime | None) -> datetime | None:
    """连续失败次数达到 LOCK_THRESHOLD 时，锁定到 last_failure + LOCK_DURATION；否则 None。"""
    if failures >= LOCK_THRESHOLD and last_failure is not None:
        return last_failure + LOCK_DURATION
    return None
