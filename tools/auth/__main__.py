"""账号管理命令行（批次 16a）。只在数据库模式下使用（HAINA_PG_DSN、HAINA_PG_SCHEMA 从环境读取）。

    python -m tools.auth create-admin --username admin --display-name 管理员
    python -m tools.auth reset-password --username zhang
    python -m tools.auth list-users

密码只从终端交互输入（输入两次，不回显），不接受命令行参数或环境变量，也不写入任何文件或日志。
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Callable

from core.domain.identity import normalize_username, password_problem


def _read_password(username: str, prompt: Callable[[str], str]) -> str:
    first = prompt("新密码：")
    problem = password_problem(first, username=username)
    if problem is not None:
        raise ValueError(problem)
    if prompt("再输入一次：") != first:
        raise ValueError("两次输入的密码不一致")
    return first


def main(argv: list[str] | None = None, *, prompt: Callable[[str], str] = getpass.getpass) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.auth", description="海纳知识库账号管理")
    sub = parser.add_subparsers(dest="cmd", required=True)
    admin = sub.add_parser("create-admin", help="创建第一个管理员（已有管理员时拒绝）")
    admin.add_argument("--username", required=True)
    admin.add_argument("--display-name", required=True)
    reset = sub.add_parser("reset-password", help="重置某个账号的密码并清除其会话与失败计数")
    reset.add_argument("--username", required=True)
    sub.add_parser("list-users", help="列出账号（不含密码）")
    args = parser.parse_args(argv)
    dsn = os.environ.get("HAINA_PG_DSN")
    if not dsn:
        print("需要设置 HAINA_PG_DSN")
        return 2
    from core.adapters.passwords import ScryptHasher
    from core.adapters.pg_identity import PgIdentityRepo
    from core.adapters.pg_registry import migrate
    from core.app.identity import AuthService, Forbidden
    from apps.web.jobs import china_now
    schema = os.environ.get("HAINA_PG_SCHEMA") or "public"
    try:
        migrate(dsn, schema=schema)
        repo = PgIdentityRepo(dsn, schema=schema)
    except Exception as exc:
        print(f"无法连接数据库：{type(exc).__name__}".replace(dsn, "<redacted>"))
        return 1
    try:
        service = AuthService(repo, ScryptHasher(), clock=china_now)
        if args.cmd == "list-users":
            for user in repo.users():
                print(f"{user.username}\t{user.display_name}\t{user.role.value}\t{'启用' if user.active else '停用'}")
            return 0
        username = normalize_username(args.username)
        if args.cmd == "create-admin":
            user = service.bootstrap_admin(username, args.display_name, _read_password(username, prompt))
            print(f"已创建管理员 {user.username}")
            return 0
        user = repo.user_by_name(username)
        if user is None:
            print("账号不存在")
            return 2
        password = _read_password(username, prompt)
        repo.set_password_hash(user.user_id, ScryptHasher().hash(password))
        repo.reset_failures(user.user_id)
        repo.delete_user_sessions(user.user_id)
        repo.append_audit(china_now(), None, "password_reset_cli", user.user_id, {})
        print(f"已重置 {user.username} 的密码")
        return 0
    except (ValueError, Forbidden) as exc:
        print(str(exc))
        return 2
    finally:
        repo.close()


if __name__ == "__main__":
    sys.exit(main())
