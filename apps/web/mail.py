"""邮件：扫描作业与接口（批次 17a）。规格见 docs/spec-30-mail.md 第六、七节。

邮件只在数据库模式下启用（扫描目录在 PostgreSQL，检索索引在 Elasticsearch）；没有配置邮件根目录时不注册扫描，
检索接口返回 404。
"""
from __future__ import annotations

import copy
import threading
import time
from dataclasses import asdict
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, StrictBool, StrictStr

from apps.web.jobs import china_now
from apps.web.scan import MIN_INTERVAL_MINUTES, ScanBusy, parse_scan_interval, parse_scan_roots
from apps.web.services import Redactor
from core.adapters.local_mail import LocalMailTree
from core.adapters.memory import InMemoryTaskRepo
from core.app.mail import (COUNTS, MailCatalog, MailHit, MailIndex, MailScanService, ScanInterrupted, hit_from_doc,
                           merge_reports)
from core.app.mail_qa import MailQaService, mail_thread
from core.app.ports import PermanentError, Task, TaskQueue, TaskStatus, TransientError
from core.app.worker import Beat, TaskFailed, Worker
from core.domain.mail import MailDoc
from core.domain.mail_query import build_query

MAIL_SCAN = "mail_scan"
FOLDER_DEPTH = 3            # 文件夹授权页列出的邮件文件夹层数（含根名称）


@dataclass(frozen=True)
class MailParts:
    """create_app 的邮件部件。roots：{名称: 目录}；interval_minutes：定时扫描间隔（None 为不定时）。"""
    index: MailIndex
    catalog: MailCatalog
    roots: Mapping[str, Path]
    interval_minutes: int | None = None


def parse_mail_settings(roots: str | None, interval: str | None, *,
                        document_roots: Mapping[str, Path] = {}) -> tuple[dict[str, Path], int | None]:
    """HAINA_MAIL_ROOTS（格式同 HAINA_SCAN_ROOTS）与 HAINA_MAIL_SCAN_INTERVAL_MINUTES。
    邮件根的名称与文档扫描根的名称相同 → ValueError（两者共用文件夹授权的命名空间，同名会让授权互相影响）。"""
    parsed = parse_scan_roots(roots, variable="HAINA_MAIL_ROOTS")
    clash = sorted(set(parsed) & set(document_roots))
    if clash:
        raise ValueError(f"邮件根与文档扫描根同名：{', '.join(clash)}")
    return parsed, parse_scan_interval(interval, variable="HAINA_MAIL_SCAN_INTERVAL_MINUTES")


class MailJobs:
    """邮件扫描作业（任务种类 "mail_scan"，租约 1800 秒并按进度续租）。结构与 ScanJobs（批次 15d）相同。

    批次 17b 起按分片并行：enqueue 放入计划任务（payload {"root","scan_id","full","stage":"plan"}，幂等键
    f"mail_scan:{scan_id}"）；计划任务执行时保存整次扫描的报告（"parts": 分片数）并放入各分片任务（payload
    {"root","scan_id": f"{scan_id}.{序号:05d}","parent": scan_id,"full","stage":"part","part": ""或子目录名}，
    幂等键 f"mail_scan:{scan_id}:{序号:05d}"），分片可由多个 worker 线程或进程同时执行。
    status / runs / run 中的整次扫描报告为 merge_reports 合并后的结果，另加 "parts_failed"；
    active 为 {"task_id","status","scan_id": 整次扫描的,"full","pending": 尚未完成的任务数,"progress"}。
    （下面是 17a 的说明，除上述变化外不变）

    - enqueue(root, *, full=False)：根不在配置中 → KeyError；该根已有排队或执行中的扫描 → ScanBusy；否则入队返回任务 ID。
      scan_id 为 f"{china_now():%Y%m%d%H%M%S}-{uuid4().hex[:8]}"。
    - handle：MailScanService(index, catalog, clock=china_now).run(根名称, tree_factory(根目录), …)；
      progress 经 beat 报告；close() 期间被要求停止 → TransientError（任务回到队列）；PermanentError → TaskFailed(脱敏消息)。
    - status()：{"roots": [{"name", "path", "active": {"task_id","status","progress","scan_id","full"} 或 None,
      "last_run": 最近一份报告去掉 "items" 或 None, "stats": catalog.stats(名称)}…按名称], "interval_minutes"}。
    - runs(root, limit)：根不在配置中 → KeyError；返回去掉 "items" 的报告。run(scan_id)：完整报告或 None。
    - raw(doc)：读取邮件原件；根已不在配置中或文件不存在、读取失败 → FileNotFoundError。
    - start_schedule(interval_minutes)：每隔 interval 分钟对全部根做一次增量扫描（忙的跳过）；close() 停止。"""

    KIND = MAIL_SCAN

    def __init__(self, roots: Mapping[str, Path], index: MailIndex, catalog: MailCatalog, redactor: Redactor, *,
                 tasks: TaskQueue | None = None, run_workers: bool = True, worker_id: str = "web",
                 poll_seconds: float = 1.0, tree_factory: Callable[[Path], Any] = LocalMailTree,
                 minute_seconds: float = 60.0) -> None:
        self._roots = dict(roots)
        self._index, self._catalog, self._redactor = index, catalog, redactor
        self._tree_factory, self._minute = tree_factory, minute_seconds
        self._tasks = tasks if tasks is not None else InMemoryTaskRepo(china_now)
        self._run_workers = run_workers
        self._stop = threading.Event()
        self._lock = RLock()
        self._interval: int | None = None
        self._schedule: threading.Thread | None = None
        self.worker = Worker(self._tasks, {self.KIND: self.handle}, worker_id=f"{worker_id}-mail", lease_seconds=1800,
                             poll_seconds=poll_seconds, redact=redactor.error)

    @property
    def roots(self) -> dict[str, Path]:
        return dict(self._roots)

    @property
    def index(self) -> MailIndex:
        return self._index

    @property
    def catalog(self) -> MailCatalog:
        return self._catalog

    def start(self) -> None:
        if self._run_workers:
            self.worker.start()

    def close(self) -> None:
        self._stop.set()
        self.worker.close()
        if self._schedule is not None and self._schedule is not threading.current_thread():
            self._schedule.join()

    def _active(self, root: str) -> Task | None:
        return next((task for task in self._tasks.tasks(self.KIND, statuses={TaskStatus.QUEUED, TaskStatus.RUNNING},
                                                       limit=10_000) if task.payload.get("root") == root), None)

    def enqueue(self, root: str, *, full: bool = False) -> str:
        if root not in self._roots:
            raise KeyError(root)
        with self._lock:
            active = self._active(root)
            if active is not None:
                raise ScanBusy(active.task_id)
            scan_id = f"{china_now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"
            task = self._tasks.enqueue(f"{self.KIND}:{scan_id}", self.KIND,
                                       {"root": root, "scan_id": scan_id, "full": bool(full), "stage": "plan"})
        self.worker.notify()
        return task.task_id

    def handle(self, task: Task, beat: Beat) -> None:
        root, scan_id = task.payload["root"], task.payload["scan_id"]
        if root not in self._roots:
            raise TaskFailed("邮件根已不在配置中")
        service = MailScanService(self._index, self._catalog, clock=china_now)
        full, stage = bool(task.payload.get("full")), task.payload.get("stage")
        try:
            tree = self._tree_factory(self._roots[root])
            if stage == "plan":
                self._plan(service, tree, root, scan_id, full)
                return
            service.run(root, tree, scan_id=scan_id, full=full, progress=lambda text: beat(text),
                        should_stop=self._stop.is_set, part=task.payload.get("part"),
                        parent=task.payload.get("parent"))
        except ScanInterrupted as exc:
            raise TransientError("邮件扫描被中断，稍后继续") from exc
        except PermanentError as exc:
            raise TaskFailed(self._redactor.error(exc)) from exc

    def _plan(self, service: MailScanService, tree: Any, root: str, scan_id: str, full: bool) -> None:
        """计划任务：分片为根目录本身（""）与每个直接子目录；先保存整次扫描的报告（"parts": 分片数），再逐个入队。
        重复执行（任务重试）时幂等：报告整体替换，分片任务按幂等键不会重复。"""
        parts = ["", *service.plan(root, tree, full=full)]
        self._catalog.save_run({"scan_id": scan_id, "root": root, "full": full, "part": None,
                                "started_at": china_now().isoformat(), "finished_at": None, "parts": len(parts),
                                "counts": dict.fromkeys(COUNTS, 0), "items": [], "truncated": False})
        for index, name in enumerate(parts):
            self._tasks.enqueue(f"{self.KIND}:{scan_id}:{index:05d}", self.KIND,
                                {"root": root, "scan_id": f"{scan_id}.{index:05d}", "parent": scan_id, "full": full,
                                 "stage": "part", "part": name})
        self.worker.notify()

    def _merged(self, report: dict[str, Any] | None) -> dict[str, Any] | None:
        """整次分片扫描的报告与其分片合并（merge_reports），并加上 parts_failed（失败的分片任务数）；
        不是分片扫描（17a 的整树扫描报告，或分片报告本身）时原样返回。"""
        if report is None or "parts" not in report:
            return copy.deepcopy(report)
        merged = merge_reports(report, self._catalog.parts(report["scan_id"]))
        merged["parts_failed"] = sum(task.payload.get("parent") == report["scan_id"]
                                     for task in self._tasks.tasks(self.KIND, statuses={TaskStatus.FAILED},
                                                                   limit=10_000))
        return merged

    def status(self) -> dict[str, Any]:
        rows = []
        for name, path in sorted(self._roots.items()):
            active = self._active(name)
            last = self._catalog.runs(name, 1)
            merged = self._merged(last[0]) if last else None
            summary = None
            if active is not None:
                scan_id = active.payload.get("parent") or active.payload["scan_id"]
                running = [task for task in self._tasks.tasks(self.KIND, statuses={TaskStatus.QUEUED, TaskStatus.RUNNING},
                                                             limit=10_000)
                           if (task.payload.get("parent") or task.payload["scan_id"]) == scan_id]
                summary = {"task_id": active.task_id, "status": active.status.value, "scan_id": scan_id,
                           "full": bool(active.payload.get("full")), "pending": len(running),
                           "progress": "；".join(task.progress for task in running
                                                 if task.status is TaskStatus.RUNNING and task.progress) or None}
            rows.append({"name": name, "path": str(path), "stats": self._catalog.stats(name),
                         "last_run": {key: value for key, value in merged.items() if key != "items"} if merged else None,
                         "active": summary})
        return {"roots": rows, "interval_minutes": self._interval}

    def runs(self, root: str, limit: int = 20) -> list[dict[str, Any]]:
        if root not in self._roots:
            raise KeyError(root)
        return [{key: value for key, value in self._merged(row).items() if key != "items"}
                for row in self._catalog.runs(root, limit)]

    def run(self, scan_id: str) -> dict[str, Any] | None:
        return self._merged(self._catalog.run(scan_id))

    def raw(self, doc: MailDoc) -> bytes:
        if doc.root not in self._roots:
            raise FileNotFoundError("邮件根已不在配置中")
        folder, _, name = doc.path.rpartition("/")
        try:
            tree = self._tree_factory(self._roots[doc.root])
            return tree.read(folder, name)
        except (OSError, PermanentError) as exc:
            raise FileNotFoundError("原件已不在邮件目录中") from exc

    def start_schedule(self, interval_minutes: int) -> None:
        if interval_minutes < MIN_INTERVAL_MINUTES:
            raise ValueError("定时扫描间隔过短")
        if self._schedule is not None:
            return
        self._interval = interval_minutes

        def loop() -> None:
            while not self._stop.wait(interval_minutes * self._minute):
                for name in sorted(self._roots):
                    try:
                        self.enqueue(name)
                    except Exception:
                        pass

        self._schedule = threading.Thread(target=loop, name="haina-mail-schedule", daemon=True)
        self._schedule.start()


class _MailAskRequest(BaseModel):
    question: StrictStr = Field(min_length=1, max_length=500)
    sender: StrictStr = ""
    recipient: StrictStr = ""
    start: StrictStr = ""
    end: StrictStr = ""
    folder: StrictStr = ""
    attachments: StrictStr = ""


class _MailScanRequest(BaseModel):
    root: StrictStr
    full: StrictBool = False


def hit_json(hit: MailHit) -> dict[str, Any]:
    return {"mail_id": hit.mail_id, "folder": hit.folder, "subject": hit.subject,
            "sender": hit.sender.to_json() if hit.sender else None, "to": [a.to_json() for a in hit.to],
            "recipients": hit.recipients, "sent_at": hit.sent_at, "snippet": hit.snippet,
            "attachments": hit.attachments, "size": hit.size, "message_id": hit.message_id}


def register(app: FastAPI, mail: MailJobs | None, *, scope_of: Callable[[Any], Any], response: Callable[..., JSONResponse],
             current: Callable[[Request], Any], llm: Any = None,
             record_unanswered: Callable[[str, str], Any] | None = None,
             redact: Callable[[str], str] = lambda text: text) -> None:
    """注册 /api/mail/*。mail 为 None（未启用邮件）时全部返回 404 {"error": "未启用邮件"}。
    批次 17b：llm 为模型客户端（问邮件；为 None 时 /api/mail/ask 返回 404）；record_unanswered(问题, 状态) 记录未收录问题
    （状态为 "mail_no_hits" 或 "mail_insufficient"），问题先经 redact 脱敏。

    scope_of(access)：当前账号的 FolderScope（开放模式为 None，表示不限）。
    权限（apps/web/auth.py）：/api/mail/scan… 需要 SCAN；其余需要 READ，内容再按 FolderScope 过滤。"""

    def enabled() -> MailJobs:
        if mail is None:
            raise HTTPException(404, "未启用邮件")
        return mail

    def readable(mail_id: str, access: Any) -> MailDoc:
        jobs = enabled()
        try:
            doc = jobs.index.get(mail_id)
        except TransientError:
            raise HTTPException(503, "邮件检索暂时不可用") from None
        scope = scope_of(access)
        if doc is None or (scope is not None and not scope.contains(doc.folder)):
            raise HTTPException(404)
        return doc

    @app.get("/api/mail/search")
    def search(q: str = "", sender: str = "", recipient: str = "", start: str = "", end: str = "", folder: str = "",
               attachments: str = "", offset: int = 0, limit: int = 20, access: Any = Depends(current)) -> JSONResponse:
        """{"rows": [hit_json…], "total", "total_exact", "offset", "limit"}。条件不合规 → 400 {"error"}（中文，可直接显示）；
        索引暂时不可用 → 503。"""
        jobs = enabled()
        try:
            query = build_query(q=q, sender=sender, recipient=recipient, start=start, end=end, folder=folder,
                                attachments=attachments)
            page = jobs.index.search(query, scope=scope_of(access), offset=offset, limit=limit)
        except ValueError as exc:
            return response({"error": str(exc)}, 400)
        except TransientError:
            return response({"error": "邮件检索暂时不可用"}, 503)
        return response({"rows": [hit_json(hit) for hit in page.rows], "total": page.total,
                         "total_exact": page.total_exact, "offset": offset, "limit": limit})

    @app.get("/api/mail/scan")
    def scan_status() -> JSONResponse:
        return response(enabled().status())

    @app.post("/api/mail/scan")
    def start_scan(body: _MailScanRequest) -> JSONResponse:
        """{"root", "full"} → 202 {"task_id"}；根不存在 → 404；该根正在扫描 → 409 {"error","task_id"}。"""
        jobs = enabled()
        try:
            task_id = jobs.enqueue(body.root, full=body.full)
        except KeyError:
            raise HTTPException(404, "没有这个邮件目录") from None
        except ScanBusy as exc:
            return response({"error": "该邮件目录正在扫描", "task_id": exc.task_id}, 409)
        return response({"task_id": task_id}, 202)

    @app.get("/api/mail/scan/runs")
    def scan_runs(root: str, limit: int = 20) -> JSONResponse:
        jobs = enabled()
        try:
            return response({"runs": jobs.runs(root, limit)})
        except KeyError:
            raise HTTPException(404) from None
        except ValueError as exc:
            return response({"error": str(exc)}, 400)

    @app.get("/api/mail/scan/runs/{scan_id}")
    def scan_run(scan_id: str) -> JSONResponse:
        jobs = enabled()
        report = jobs.run(scan_id)
        if report is None or report["root"] not in jobs.roots:
            raise HTTPException(404)
        return response(report)

    @app.post("/api/mail/ask")
    def ask(body: _MailAskRequest, access: Any = Depends(current)) -> JSONResponse:
        """小安问邮件（批次 17b）：{"question", 可选的 sender、recipient、start、end、folder、attachments（同检索）} →
        MailQaResult 的字段（asdict）加 "seconds"。条件或问题不合规 → 400；检索索引或模型暂时不可用 → 503，
        永久错误 → 502（全局的 TransientError / PermanentError 处理，消息已脱敏）。没有命中或资料不足时记录未收录问题。"""
        jobs = enabled()
        if llm is None:
            raise HTTPException(404)
        started = time.perf_counter()
        try:
            query = build_query(sender=body.sender, recipient=body.recipient, start=body.start, end=body.end,
                                folder=body.folder, attachments=body.attachments)
            result = MailQaService(jobs.index, llm).ask(body.question, query, scope=scope_of(access))
        except ValueError as exc:
            return response({"error": str(exc)}, 400)
        if record_unanswered is not None and result.status in ("no_hits", "insufficient"):
            record_unanswered(redact(result.question), f"mail_{result.status}")
        return response({**asdict(result), "seconds": round(time.perf_counter() - started, 1)})

    @app.get("/api/mail/{mail_id}")
    def detail(mail_id: str, access: Any = Depends(current)) -> JSONResponse:
        """MailDoc.to_json()；不存在或不可读 → 404（二者不区分）。"""
        return response(readable(mail_id, access).to_json())

    @app.get("/api/mail/{mail_id}/thread")
    def thread(mail_id: str, access: Any = Depends(current)) -> JSONResponse:
        """会话线索（批次 17b）：{"root", "current": mail_id, "rows": [hit_json…]（mail_thread 去重后的顺序）, "truncated"}；
        当前邮件不存在或不可读 → 404；只列出当前账号可读的成员。"""
        doc = readable(mail_id, access)
        try:
            result = mail_thread(enabled().index, doc, hit_from_doc(doc), scope=scope_of(access))
        except TransientError:
            return response({"error": "邮件检索暂时不可用"}, 503)
        return response({"root": result.root, "current": mail_id, "rows": [hit_json(row) for row in result.rows],
                         "truncated": result.truncated})

    @app.get("/api/mail/{mail_id}/raw")
    def raw(mail_id: str, access: Any = Depends(current)) -> Response:
        """邮件原件（message/rfc822，作为附件下载，文件名为原文件名）；不可读 → 404；原件已不在 → 410。"""
        doc = readable(mail_id, access)
        try:
            data = enabled().raw(doc)
        except FileNotFoundError:
            return response({"error": "原件已不在邮件目录中"}, 410)
        name = doc.path.rpartition("/")[2]
        return Response(data, media_type="message/rfc822",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
