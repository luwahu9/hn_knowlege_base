"""本机 Web 启动入口；服务凭据仅从启动进程的环境读取。"""
from __future__ import annotations

import argparse
from pathlib import Path

import httpx
import uvicorn

from apps.web.api import create_app
from core.adapters.gpustack_llm import GpustackLlm
from apps.web.assembly import (account_parts, build_services, check_embedding, location_problems,
                                mail_parts_from_environment, missing_environment, open_stores, redactor_from_environment,
                                scan_settings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apps.web", description="海纳知识库本机 Web 界面")
    parser.add_argument("--data", default="kb_data", help="本地数据目录")
    parser.add_argument("--seed-from", help="首次启动使用的批次 7a 运行目录")
    parser.add_argument("--host", default="127.0.0.1", help="仅允许 127.0.0.1")
    parser.add_argument("--port", default=8600, type=int)
    parser.add_argument("--open", action="store_true",
                        help="数据库模式下也不启用账号（任何能访问本机端口的人都能读全部文档），仅供单人试用")
    parser.add_argument("--secure-cookie", action="store_true", help="会话 Cookie 带 Secure（经 HTTPS 反向代理访问时使用）")
    parser.add_argument("--no-workers", action="store_true",
                        help="不在 Web 进程中执行导入、抽取、连线作业（需另行启动 python -m apps.worker，仅数据库模式）")
    args = parser.parse_args(argv)
    if args.host != "127.0.0.1" or not 1 <= args.port <= 65535:
        print("只允许监听 127.0.0.1，端口须在 1 至 65535 之间")
        return 2
    missing = missing_environment()
    if missing:
        print("缺少环境变量：" + ", ".join(missing))
        return 2
    problems, model_location = location_problems()
    if problems:
        print("\n".join(problems))
        return 2
    if model_location == "online":
        print("提示：当前使用在线服务，仅限样例数据（HAINA_DATA_PROFILE=sample）", flush=True)
    secrets, redactor = redactor_from_environment()
    try:
        parts = open_stores(Path(args.data).resolve(), seed_from=args.seed_from)
        if args.no_workers and "tasks" not in parts:
            raise ValueError("--no-workers 只能在数据库模式（设置 HAINA_PG_DSN）下使用")
        store, data_root = parts.pop("store"), parts.pop("data_root")
        identity, permission_repo = parts.pop("identity", None), parts.pop("permission_repo", None)
        gate = None
        if identity is not None and not args.open:
            from apps.web.auth import AccountGate
            gate = AccountGate(*account_parts(store, identity, permission_repo))
            if not any(user.role.value == "admin" for user in identity.users()):
                print("提示：尚未创建管理员账号，请先运行 python -m tools.auth create-admin", flush=True)
        elif identity is not None:
            print("警告：--open 已关闭账号与文件夹授权，任何能访问本机端口的人都能读取全部文档", flush=True)
        roots, interval = scan_settings()
        mail = mail_parts_from_environment({**parts, "store": store})
        with httpx.Client() as http:
            check_embedding(store.dataset_id, http)                     # 批次 21b
            app = create_app(store, build_services(store, data_root, http,
                                                 llm_factory=lambda *a, **k: GpustackLlm(*a, **k)), secrets=secrets, auto_links=True,
                             data_root=data_root, run_workers=not args.no_workers, scan_roots=roots,
                             scan_interval_minutes=interval, gate=gate, secure_cookie=args.secure_cookie, mail=mail,
                             model_location=model_location, **parts)
            # 不记录请求 URL，避免用户把凭据误填进查询框后进入访问日志。
            uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
        return 0
    except (ValueError, FileNotFoundError) as exc:
        print(redactor.error(exc))
        return 2
    except Exception as exc:
        print(redactor.error(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
