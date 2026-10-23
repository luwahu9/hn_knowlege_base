"""导入作业：入队（任务队列）、分配版本号、调用已有入库用例并保存结果。"""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any

from apps.web.services import Redactor, Services
from apps.web.store import now_iso
from core.app.registry import DocumentRegistry
from core.domain.folders import normalize_folder_path
from core.app.ingest import Ingestor, IngestRequest, IngestStatus, OCR_EXTENSIONS, extension_of
from core.domain.file_kind import effective_extension, is_encrypted_office
from core.adapters.memory import InMemoryTaskRepo
from core.app.ports import Chunk, Indexer, IndexReceipt, PermanentError, Task, TaskQueue, TaskStatus, TransientError
from core.app.permissions import IndexTagger, LegacyTagger
from core.app.worker import Beat, TaskFailed, Worker
from apps.runtime import ANSWER_FILE, title_for, version_id_for
from core.adapters.docling_http import SUPPORTED_EXTENSIONS
from core.adapters.fs_blob import validate_key


class DuplicateUpload(Exception):
    def __init__(self, version_id: str, job_id: str | None = None) -> None:
        super().__init__("该文件已导入")
        self.version_id = version_id
        self.job_id = job_id


def stale_ingest_task(tasks: TaskQueue, digest: str) -> bool:
    """幂等键 f"ingest:{digest}" 的任务是否存在且已结束（DONE 或 FAILED）。调用时已确认该内容没有登记的文档。"""
    task = tasks.find(f"{INGEST}:{digest}")
    return task is not None and task.status in (TaskStatus.DONE, TaskStatus.FAILED)


def upload_name_error(filename: str) -> str | None:
    """导入文件名检查（上传、文件夹上传、目录扫描共用）：返回中文错误原因，合规为 None。
    依次：为空或含路径分隔符、是答案文件、扩展名不在 SUPPORTED_EXTENSIONS、不能作为原件存储键的一部分。"""
    if not filename or "/" in filename or "\\" in filename or filename == ANSWER_FILE:
        return "不允许导入该文件"
    if extension_of(filename) not in SUPPORTED_EXTENSIONS:
        return "暂不支持该格式"
    try:
        validate_key("raw/" + filename)
    except PermanentError:
        return "文件名含有不支持的字符"
    return None


class _Unsupported(Exception):
    """按文件头无法识别格式：记为 unsupported，不调用入库用例。"""


class _JobIndexer:
    """限定入库用例只能发布当前作业新分配的版本。"""
    def __init__(self, indexer: Indexer, version_id: str, protected: frozenset[str]) -> None:
        self._indexer, self._version, self._protected = indexer, version_id, protected

    def publish(self, version_id: str, chunks: Sequence[Chunk], group_tags: Sequence[str]) -> IndexReceipt:
        if version_id != self._version or version_id in self._protected:
            raise PermanentError("禁止发布运行前已有版本或其他作业的版本")
        return self._indexer.publish(version_id, chunks, group_tags)


INGEST = "ingest"
RETRYABLE_STATUSES = ("error", "qc_blocked", "unsupported")
_JOB_STATUS = {TaskStatus.QUEUED: "queued", TaskStatus.RUNNING: "running", TaskStatus.DONE: "done",
               TaskStatus.FAILED: "failed"}


def china_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


class JobManager:
    """导入作业（批次 15c 起基于任务队列，规格见 docs/spec-26-task-queue.md）。

    - submit：先把原件写入 blobs，分配版本号，再以 f"ingest:{sha256}" 为幂等键入队；同一内容只会有一个导入任务。
      作业 ID 即任务 ID。已登记相同内容 → DuplicateUpload(版本号)；同一内容的任务已存在（任何状态）→
      DuplicateUpload(该任务的版本号, 任务 ID)。
    - retry：对 error / qc_blocked / unsupported 的文档，用同一原件以新版本号重新导入（幂等键
      f"ingest:{sha256}:retry:{原版本}"，同一版本重复调用返回同一个作业），并在原记录上写 review
      {"action": "retried", "retry_version": 新版本, "at": 时间}。
    - handle：任务处理器，行为与批次 14 的单线程导入相同；版本已登记时直接返回（进程中断后重新领取的情形）。
      入库用例抛出异常时先保存 status 为 error 的记录，再以 TaskFailed 结束任务。
    - run_workers 为 False 时只入队、不在本进程执行（由独立 worker 进程执行，见 apps/worker）。"""

    def __init__(self, store: DocumentRegistry, services: Services, redactor: Redactor, *, retry_delay: float = 5.0,
                 on_published: Callable[[str], Any] | None = None, tasks: TaskQueue | None = None,
                 run_workers: bool = True, worker_id: str = "web", poll_seconds: float = 1.0,
                 tagger: IndexTagger | None = None) -> None:
        """tagger：检索索引标签（批次 16a）；缺省为 LegacyTagger(store.group_tag)。"""
        self._store, self._services, self._redactor = store, services, redactor
        self._tagger = tagger if tagger is not None else LegacyTagger(store.group_tag)
        self._on_published = on_published
        self._retry_delay = retry_delay
        self._tasks = tasks if tasks is not None else InMemoryTaskRepo(china_now)
        self._run_workers = run_workers
        self._lock = RLock()
        self.worker = Worker(self._tasks, {INGEST: self.handle}, worker_id=f"{worker_id}-ingest", lease_seconds=1800,
                             poll_seconds=poll_seconds, redact=redactor.error)

    @property
    def tasks(self) -> TaskQueue:
        return self._tasks

    def start(self) -> None:
        if self._run_workers:
            self.worker.start()

    def close(self) -> None:
        self.worker.close()

    def lookup(self, sha256: str) -> dict[str, Any] | None:
        """按原件 sha256 查是否已导入（批次 15d）：已登记 → {"state": "exists", "version_id"}；
        有排队或执行中的导入任务 → {"state": "importing", "version_id", "job_id"}；否则 None。"""
        version = self._store.find_by_digest(sha256)
        if version is not None:
            return {"state": "exists", "version_id": version}
        task = self._tasks.find(f"{INGEST}:{sha256.lower()}")
        if task is not None and task.status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
            return {"state": "importing", "version_id": task.payload["version_id"], "job_id": task.task_id}
        return None

    def submit(self, filename: str, data: bytes, *, folder: str = "", origin: str = "upload",
               by: str | None = None) -> str:
        """folder：导入时的相对文件夹路径，先规范化（非法 → ValueError）；filename 不合规 → ValueError（见 upload_name_error）。
        by：提交者 user_id（批次 16a，记入任务 payload，用于作业查询的权限判断）。
        返回作业 ID。查重用 lookup（按索引查询，不遍历全部文档与任务）。"""
        folder = normalize_folder_path(folder)
        problem = upload_name_error(filename)
        if problem is not None:
            raise ValueError(problem)
        digest = hashlib.sha256(data).hexdigest()
        found = self.lookup(digest)
        if found is not None:
            raise DuplicateUpload(found["version_id"], found.get("job_id"))
        key = f"raw/{digest}/{filename}"
        self._services.blobs.put(key, data)
        # 先查过进行中的同内容任务再分配版本号，避免无谓地消耗序号；并发提交时仍可能留下空号（序号不要求连续）。
        version = version_id_for(self._store.next_version_number(), digest)
        # 同内容的旧任务已结束却没有留下文档记录（例如租约三次耗尽）：换一个幂等键重新导入，否则这份文件永远无法再导入。
        stale = stale_ingest_task(self._tasks, digest)
        task_key = f"{INGEST}:{digest}:{version}" if stale else f"{INGEST}:{digest}"
        task = self._tasks.enqueue(task_key, INGEST, {
            "filename": filename, "folder": folder, "sha256": digest, "blob_key": key, "version_id": version,
            "origin": origin, "retry_of": None, "by": by})
        if task.payload["version_id"] != version:
            raise DuplicateUpload(task.payload["version_id"], task.task_id)
        self.worker.notify()
        return task.task_id

    def owner(self, job_id: str) -> tuple[str | None, str] | None:
        """(提交者 user_id 或 None, version_id)；作业不存在或不是导入作业 → None（批次 16a）。"""
        try:
            task = self._tasks.get(job_id)
        except PermanentError:
            return None
        return None if task.kind != INGEST else (task.payload.get("by"), task.payload["version_id"])

    def retry(self, version_id: str, *, by: str | None = None) -> str:
        """重新导入一个失败的文档，返回作业 ID。版本不存在 → KeyError；状态不可重试、缺少原件 → ValueError。"""
        detail = self._store.detail(version_id)
        if detail is None:
            raise KeyError(version_id)
        record = detail["document"]
        if record["status"] not in RETRYABLE_STATUSES:
            raise ValueError("只能重试导入失败、质检未通过或格式不支持的文档")
        digest, filename = record.get("sha256"), record["filename"]
        key = f"raw/{digest}/{filename}"
        if not digest or not self._services.blobs.exists(key):
            raise ValueError("该文档缺少原件，无法重试")
        retry_key = f"{INGEST}:{digest}:retry:{version_id}"
        with self._lock:
            version = version_id_for(self._store.next_version_number(), digest)
            task = self._tasks.enqueue(retry_key, INGEST, {
                "filename": filename, "folder": record.get("folder", ""), "sha256": digest, "blob_key": key,
                "version_id": version, "origin": record.get("origin", "upload"), "retry_of": version_id, "by": by})
            if task.payload["version_id"] == version:
                self._store.set_review(version_id, {"action": "retried", "retry_version": version,
                                                    "at": now_iso()})
        self.worker.notify()
        return task.task_id

    def get(self, job_id: str) -> dict[str, Any] | None:
        try:
            task = self._tasks.get(job_id)
        except PermanentError:
            return None
        if task.kind != INGEST:
            return None
        payload = task.payload
        detail = self._store.detail(payload["version_id"])
        status = _JOB_STATUS[task.status]
        stage = (task.progress or "converting") if task.status is TaskStatus.RUNNING else status
        return {"job_id": task.task_id, "filename": payload["filename"], "status": status, "stage": stage,
                "document": detail["document"] if detail and task.status in (TaskStatus.DONE, TaskStatus.FAILED) else None,
                "error": task.last_error if task.status is TaskStatus.FAILED else None, "folder": payload["folder"],
                "version_id": payload["version_id"], "retry_of": payload.get("retry_of")}

    def handle(self, task: Task, beat: Beat) -> None:
        payload = task.payload
        version, filename, folder = payload["version_id"], payload["filename"], payload["folder"]
        if self._store.detail(version) is not None:
            return
        started = time.perf_counter()
        data = self._services.blobs.get(payload["blob_key"])
        digest = payload["sha256"]
        record = {"rel": filename, "filename": filename, "version_id": version, "title": title_for(filename),
                  "status": "error", "detail": "", "error": None, "ocr": extension_of(filename) in OCR_EXTENSIONS,
                  "page_count": None, "qc": [], "chunk_count": 0, "engine_doc_id": None,
                  "verify": None, "verify_error": None, "seconds": 0.0, "imported_at": now_iso(),
                  "origin": payload.get("origin", "upload"), "sha256": digest, "extension": None, "content_hash": None,
                  "period": None, "restructure": None, "duplicate_of": None, "folder": folder}
        if payload.get("retry_of"):
            record["retry_of"] = payload["retry_of"]
        rows: list[dict[str, Any]] = []
        beat("converting")
        error = None
        try:
            key = payload["blob_key"]
            extension = effective_extension(data[:16], filename)
            record["extension"] = extension
            if is_encrypted_office(data):
                raise _Unsupported("文件已加密，请解除打开密码后重新导入")
            if extension is None:
                raise _Unsupported("无法识别文件格式")
            guarded = _JobIndexer(self._services.indexer, version, self._store.startup_versions)
            folders = tuple(folder.split("/")) if folder else ()
            tags = tuple(self._tagger.tags(version, folder))
            req = IngestRequest(version, key, filename, record["title"], tags, folders=folders,
                                extension=extension)
            ingestor = Ingestor(self._services.blobs, self._services.converter, guarded,
                                config_hash=self._services.config_hash, find_duplicate=self._store.find_by_hash)
            for attempt in range(3):
                try:
                    outcome = ingestor.ingest(req)
                    break
                except TransientError:
                    if attempt == 2:
                        raise
                    time.sleep(self._retry_delay)
            record.update(status=outcome.status.value, detail=outcome.detail, ocr=outcome.ocr,
                          page_count=outcome.conversion.page_count if outcome.conversion else None,
                          qc=[asdict(finding) for finding in outcome.qc.findings] if outcome.qc else [],
                          chunk_count=len(outcome.chunks),
                          engine_doc_id=outcome.receipt.engine_doc_id if outcome.receipt else None,
                          content_hash=outcome.content_hash, duplicate_of=outcome.duplicate_of,
                          period=outcome.period.to_json() if outcome.period else None,
                          restructure=outcome.restructure.to_json() if outcome.restructure else None)
            if outcome.status is IngestStatus.PUBLISHED:
                rows = [{**asdict(chunk), "engine_chunk_id": outcome.receipt.chunk_map[chunk.chunk_id]}
                        for chunk in outcome.chunks]
                beat("publishing")
                try:
                    self._tagger.record(version, tags)
                except Exception:
                    pass        # 记录失败只会让下一次标签同步多核对一次
                try:
                    record["verify"] = asdict(self._services.indexer.verify(version))
                except Exception as exc:
                    record["verify_error"] = self._redactor.error(exc)
        except _Unsupported as exc:
            record.update(status=IngestStatus.UNSUPPORTED.value, detail=str(exc))
        except Exception as exc:
            error = self._redactor.error(exc)
            record.update(status="error", error=error, detail=error)
        record["seconds"] = round(time.perf_counter() - started, 1)
        record = self._redactor.clean(record)
        try:
            self._store.save_document(record, self._redactor.clean(rows))
        except Exception as exc:
            raise TaskFailed(self._redactor.error(exc)) from exc
        if error is not None:
            raise TaskFailed(error)
        if record["status"] == "published" and self._on_published is not None:
            try:
                self._on_published(version)
            except Exception:
                # 抽取排队失败不改变已完成的入库结果。
                pass
