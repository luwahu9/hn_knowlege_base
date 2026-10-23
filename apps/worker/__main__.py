"""独立 worker 进程（批次 15c）：从数据库任务队列领取并执行导入、抽取、连线作业。规格见 docs/spec-26-task-queue.md。

用法（凭据只从环境读取，必须设置 HAINA_PG_DSN）：
    python -m apps.worker --data kb_data --kinds ingest,extract,links,scan --threads 2 --name host1

- --data 必须与 Web 进程指向同一个原件目录（blobs/）。
- 不运行知识事件分发器：事件由 Web 进程分发并入队，worker 只执行。
- SIGINT / SIGTERM：停止领取新任务，等待正在执行的任务结束后退出；被强行终止时，未完成任务的租约到期后由其他 worker 重新领取。
"""
from __future__ import annotations

import argparse
import os
import signal
import threading
from pathlib import Path
from typing import Any

import httpx

from apps.web.assembly import (build_services, check_embedding, location_problems, mail_parts_from_environment,
                                missing_environment, open_stores, redactor_from_environment, scan_settings)
from apps.web.acl_sync import AclSyncJobs
from apps.web.jobs import china_now
from apps.web.scan import ScanJobs
from apps.web.store import now_iso
from core.app.lifecycle import LifecycleService
from core.app.permissions import LegacyTagger, PermissionService, PermissionTagger
from apps.web.jobs import JobManager
from apps.web.knowledge import ExtractionJobs
from apps.web.links import LinkJobs
from core.app.extract import ExtractionService
from core.app.link import LinkService

KINDS = ("ingest", "extract", "links", "scan", "acl_sync", "mail_scan")


def build_jobs(kinds: list[str], parts: dict[str, Any], services: Any, redactor: Any, *, name: str,
               poll_seconds: float = 1.0, scan_roots: dict[str, Path] | None = None, mail: Any = None) -> list[Any]:
    """按种类构造作业对象（run_workers=True），返回列表；调用方负责 start / close。
    "scan"（批次 15d）需要 parts["scan_index"] 与 scan_roots；扫描提交的导入只入队，由 ingest 作业执行。
    "mail_scan"（批次 17a）需要 mail（MailParts）；没有时 ValueError。"""
    store, tasks = parts["store"], parts["tasks"]
    common = dict(tasks=tasks, run_workers=True, worker_id=name, poll_seconds=poll_seconds)
    jobs: list[Any] = []
    permissions = None
    if parts.get("permission_repo") is not None and not parts.get("open", False):
        # 批次 16a：账号模式下导入时按文件夹授权打检索标签，标签同步作业也可以在 worker 中执行。
        permissions = PermissionService(store, parts["permission_repo"], known_groups=lambda: (), clock=china_now)
    tagger = PermissionTagger(permissions) if permissions is not None else None
    if "ingest" in kinds:
        jobs.append(JobManager(store, services, redactor, tagger=tagger, **common))
    if "acl_sync" in kinds:
        if permissions is None:
            raise ValueError("acl_sync 只能在数据库模式下执行")
        jobs.append(AclSyncJobs(permissions, services.indexer, redactor, **common))
    if "scan" in kinds:
        submitter = JobManager(store, services, redactor, tagger=tagger, **{**common, "run_workers": False})
        lifecycle = LifecycleService(store, services.indexer, tagger or LegacyTagger(store.group_tag), now_iso)
        jobs.append(ScanJobs(scan_roots or {}, parts["scan_index"], submitter, redactor, lifecycle=lifecycle, **common))
    if "extract" in kinds:
        jobs.append(ExtractionJobs(store, parts["knowledge"], ExtractionService(services.llm), redactor, **common))
    if "links" in kinds:
        jobs.append(LinkJobs(store, parts["queries"], parts["links"], LinkService(services.llm), redactor, **common))
    if "mail_scan" in kinds:
        if mail is None:
            raise ValueError("mail_scan 需要设置 HAINA_ES_URL 与 HAINA_MAIL_ROOTS")
        from apps.web.mail import MailJobs
        mail.index.ensure()
        jobs.append(MailJobs(mail.roots, mail.index, mail.catalog, redactor, **common))
    return jobs


def main(argv: list[str] | None = None, *, stop: threading.Event | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apps.worker", description="海纳知识库作业执行进程")
    parser.add_argument("--data", default="kb_data", help="原件目录（与 Web 进程相同）")
    parser.add_argument("--kinds", default=None, help="逗号分隔：ingest、extract、links、scan、acl_sync、mail_scan")
    parser.add_argument("--threads", type=int, default=1, help="每类作业的线程数")
    parser.add_argument("--name", default="worker", help="执行者名称，用于任务的 worker_id")
    parser.add_argument("--open", action="store_true", help="与 Web 的 --open 对应：导入时使用固定检索标签")
    args = parser.parse_args(argv)
    # 未指定时执行全部种类；--open 时标签同步没有意义，从缺省种类中去掉
    default = [kind for kind in KINDS if not (args.open and kind == "acl_sync") and kind != "mail_scan"]
    kinds = default if args.kinds is None else [kind.strip() for kind in args.kinds.split(",") if kind.strip()]
    if not kinds or any(kind not in KINDS for kind in kinds) or not 1 <= args.threads <= 16:
        print("--kinds 只能是 ingest、extract、links、scan、acl_sync、mail_scan 的组合，--threads 取 1–16")
        return 2
    if not os.environ.get("HAINA_PG_DSN"):
        print("worker 进程需要数据库任务队列，请设置 HAINA_PG_DSN")
        return 2
    missing = missing_environment()
    if missing:
        print("缺少环境变量：" + ", ".join(missing))
        return 2
    problems, _location = location_problems()
    if problems:
        print("\n".join(problems))
        return 2
    _, redactor = redactor_from_environment()
    stop = stop or threading.Event()
    try:
        parts = open_stores(Path(args.data).resolve())
        if "tasks" not in parts:
            raise ValueError("worker 进程需要数据库任务队列，请设置 HAINA_PG_DSN")
        with httpx.Client() as http:
            check_embedding(parts["store"].dataset_id, http)            # 批次 21b
            services = build_services(parts["store"], parts["data_root"], http)
            roots, _ = scan_settings()
            parts["open"] = args.open
            mail = mail_parts_from_environment(parts) if "mail_scan" in kinds else None
            jobs = build_jobs(kinds, parts, services, redactor, name=args.name, scan_roots=roots, mail=mail)
            for job in jobs:
                job.worker.start(args.threads)
            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(sig, lambda *_: stop.set())
            print(f"worker {args.name} 已启动：{', '.join(kinds)}，每类 {args.threads} 个线程", flush=True)
            stop.wait()
            for job in jobs:
                job.close()
        return 0
    except (ValueError, FileNotFoundError) as exc:
        print(redactor.error(exc))
        return 2
    except Exception as exc:
        print(redactor.error(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
