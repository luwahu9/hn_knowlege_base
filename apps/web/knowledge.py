"""知识数据（实体、提及、事件）的文件存储与单线程抽取作业。规格：docs/spec-17-graph-timeline.md"""
from __future__ import annotations

import copy
import json
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from apps.web.services import Redactor
from apps.web.store import _write_json, now_iso
from core.app.registry import EXTRACTION_DONE, DocumentRegistry
from core.app.extract import DocumentKnowledge, ExtractionService
from core.adapters.memory import InMemoryTaskRepo
from core.app.ports import Chunk, TaskQueue, TaskStatus, TransientError, Task
from core.app.worker import Beat, TaskFailed, Worker
from core.domain.doc_period import period_from_json
from core.domain.entity_relations import RelationRecord
from core.domain.knowledge import EntityRecord, EventRecord, MentionRecord
from core.domain.purge import orphan_entities, scrub_entity, scrubbed

_VERSION = re.compile(r"v\d+_[0-9a-fA-F]{10}")


class KnowledgeStore:
    """数据目录下的 knowledge/ 子目录：
    - entities.json：全部实体（EntityRecord.to_json 的列表，按创建顺序）；
    - docs/<version_id>.json：{"version_id","extracted_at","report","events":[...],"mentions":[...],"relations":[...]}。
    目录不存在时视为空。所有写入都是原子替换（临时文件 + os.replace），读写在同一把锁内。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root) / "knowledge"
        self._lock = RLock()
        entity_path = self._root / "entities.json"
        self._entities = ([EntityRecord.from_json(row) for row in json.loads(entity_path.read_text(encoding="utf-8"))]
                          if entity_path.exists() else [])
        self._documents: dict[str, dict[str, Any]] = {}
        for path in sorted((self._root / "docs").glob("*.json")):
            row = json.loads(path.read_text(encoding="utf-8"))
            self._documents[row["version_id"]] = row

    def entities(self) -> list[EntityRecord]:
        """全部实体，按创建顺序。"""
        with self._lock:
            return list(self._entities)

    def extracted(self) -> set[str]:
        """已有抽取结果的 version_id 集合。"""
        with self._lock:
            return set(self._documents)

    def save(self, knowledge: DocumentKnowledge, *, extracted_at: str) -> None:
        """保存一个版本的抽取结果。
        - version_id 不符合 r"v\\d+_[0-9a-fA-F]{10}" → ValueError；新实体 ID 与已有实体重复 → ValueError；两种情况都不写任何文件。
        - 先把新实体追加写入 entities.json，再写 docs/<version_id>.json（已存在则整体替换，即重新抽取）。
        """
        with self._lock:
            if _VERSION.fullmatch(knowledge.version_id) is None:
                raise ValueError("文档版本号格式无效")
            ids = {entity.id for entity in self._entities}
            for entity in knowledge.new_entities:
                if entity.id in ids:
                    raise ValueError("实体 ID 重复")
                ids.add(entity.id)
            entities = self._entities + list(knowledge.new_entities)
            document = {"version_id": knowledge.version_id, "extracted_at": extracted_at,
                        "report": knowledge.report.to_json(),
                        "events": [event.to_json() for event in knowledge.events],
                        "mentions": [mention.to_json() for mention in knowledge.mentions],
                        "relations": [relation.to_json() for relation in knowledge.relations]}
            _write_json(self._root / "entities.json", [entity.to_json() for entity in entities])
            self._entities = entities
            _write_json(self._root / "docs" / f"{knowledge.version_id}.json", document)
            self._documents[knowledge.version_id] = document

    def save_with(self, build: Callable[[list[EntityRecord]], DocumentKnowledge], *,
                  extracted_at: str) -> DocumentKnowledge:
        """JSON 模式只有一个进程，进程内的锁即可互斥。"""
        with self._lock:
            knowledge = build(list(self._entities))
            self.save(knowledge, extracted_at=extracted_at)
            return knowledge

    def events(self) -> list[EventRecord]:
        """全部版本的事件，按 event_id 升序。"""
        with self._lock:
            return sorted((EventRecord.from_json(row) for doc in self._documents.values() for row in doc["events"]),
                          key=lambda event: event.event_id)

    def mentions(self) -> list[MentionRecord]:
        """全部版本的提及，按 (version_id, 原有顺序)。"""
        with self._lock:
            return [MentionRecord.from_json(row) for version in sorted(self._documents)
                    for row in self._documents[version]["mentions"]]

    def relations(self) -> list[RelationRecord]:
        """批次 23b：全部版本的实体关系，按 relation_id 升序（批次 23b 之前保存的文档没有 relations 键，视为空）。"""
        with self._lock:
            return sorted((RelationRecord.from_json(row) for doc in self._documents.values()
                           for row in doc.get("relations", [])), key=lambda relation: relation.relation_id)

    def document(self, version_id: str) -> dict[str, Any] | None:
        """某版本保存的完整字典（深拷贝；旧文档补上 "relations": []）；没有则 None。"""
        with self._lock:
            document = copy.deepcopy(self._documents.get(version_id))
            if document is not None:
                document.setdefault("relations", [])
            return document

    def purge_document(self, version_id: str, mapping: Mapping[str, str]) -> dict[str, Any]:
        with self._lock:
            removed = self._documents.get(version_id) or {"events": [], "mentions": []}
            remaining = {v: doc for v, doc in self._documents.items() if v != version_id}
            orphans = orphan_entities(self._entities,
                                      (MentionRecord.from_json(row) for doc in remaining.values() for row in doc["mentions"]),
                                      (EventRecord.from_json(row) for doc in remaining.values() for row in doc["events"]),
                                      mapping)
            changed = sorted(e.id for e in self._entities if e.id in orphans and not scrubbed(e))
            entities = [scrub_entity(e) if e.id in changed else e for e in self._entities]
            if changed:
                _write_json(self._root / "entities.json", [entity.to_json() for entity in entities])
                self._entities = entities
            (self._root / "docs" / f"{version_id}.json").unlink(missing_ok=True)
            self._documents = remaining
            return {"events": len(removed["events"]), "mentions": len(removed["mentions"]),
                    "relations": len(removed.get("relations", [])), "entities_scrubbed": changed}


class ExtractionJobs:
    """抽取作业（批次 15c 起基于任务队列，种类 "extract"，payload {"version_id"}）。只处理登记处中 status 为 published 的版本：
    入队时检查；执行时与保存前再各检查一次，不再是已发布则任务直接完成、不写结果（批次 20b-r2）。
    run_workers 为 False 时只入队、不在本进程执行。"""

    KIND = "extract"

    def __init__(self, store: DocumentRegistry, knowledge: KnowledgeStore, service: ExtractionService, redactor: Redactor, *,
                 retry_delay: float = 5.0, attempts: int = 3,
                 on_extracted: Callable[[str], None] | None = None, tasks: TaskQueue | None = None,
                 run_workers: bool = True, worker_id: str = "web", poll_seconds: float = 1.0) -> None:
        self._store, self._knowledge, self._service, self._redactor = store, knowledge, service, redactor
        self._retry_delay, self._attempts = retry_delay, attempts
        self._on_extracted = on_extracted
        self._lock = RLock()
        self._stop = threading.Event()
        self._tasks = tasks if tasks is not None else InMemoryTaskRepo(china_now)
        self._run_workers = run_workers
        self.worker = Worker(self._tasks, {self.KIND: self.handle}, worker_id=f"{worker_id}-extract",
                             lease_seconds=1800, poll_seconds=poll_seconds, redact=redactor.error)

    def start(self) -> None:
        if self._run_workers:
            self.worker.start()

    def close(self) -> None:
        """停止：不再开始新的版本，等待当前版本结束（重试等待可被打断）后返回。"""
        self._stop.set()
        self.worker.close()

    def enqueue(self, version_id: str, *, force: bool = False, key: str | None = None) -> bool:
        """加入队列，返回是否真的加入。
        - 版本不存在 → KeyError；存在但不是 published → ValueError。
        - 已在队列中或正在运行 → False；已有抽取结果且 force 为 False → False。
        - key：任务幂等键，缺省为每次调用唯一的键（知识事件触发时传入按事件序号生成的键，重复投递不重复入队）。
        """
        with self._lock:
            detail = self._store.detail(version_id)
            if detail is None:
                raise KeyError(version_id)
            if detail["document"]["status"] != "published":
                raise ValueError("只能抽取已发布文档")
            active = {task.payload.get("version_id") for task in
                      self._tasks.tasks(self.KIND, statuses={TaskStatus.QUEUED, TaskStatus.RUNNING}, limit=100_000)}
            if version_id in active or (not force and version_id in self._knowledge.extracted()):
                return False
            self._tasks.enqueue(key or f"{self.KIND}:{version_id}:{uuid.uuid4().hex}", self.KIND,
                                {"version_id": version_id})
            self.worker.notify()
            return True

    def enqueue_missing(self) -> int:
        """把所有 published、尚无抽取结果、且不在队列中/未在运行的版本按 version_id 升序加入队列，返回加入数量。"""
        with self._lock:
            return sum(self.enqueue(row["version_id"]) for row in sorted(self._store.documents(),
                       key=lambda row: row["version_id"]) if row["status"] == "published")

    def status(self) -> dict[str, Any]:
        """{"queued": [version_id…按入队顺序，最多 1000 个], "running": 最早开始的运行中版本或 None,
            "failed": [{"version_id","error","time"}…按 version_id 升序], "idle": 没有排队与运行中的任务}。
        failed：每个版本最近一次入队的抽取任务为 FAILED 的那些（之后再抽取成功或重新入队即不再算失败）。"""
        return queue_status(self._tasks, self.KIND, "version_id")

    def handle(self, task: Task, beat: Beat) -> None:
        version = task.payload["version_id"]
        try:
            self._extract(version)
        except Exception as exc:
            raise TaskFailed(self._redactor.error(exc)) from exc

    def _published(self, version: str) -> dict[str, Any] | None:
        detail = self._store.detail(version)
        return detail if detail is not None and detail["document"]["status"] == "published" else None

    def _extract(self, version: str) -> None:
        # 批次 20b-r2：执行时版本已不存在或不再是已发布（撤下、替代、彻底删除）→ 任务直接完成，不调用模型、不写结果；
        # 恢复发布时 document_published 事件会重新入队。模型调用结束、保存之前再核对一次，期间被撤下则丢弃结果。
        detail = self._published(version)
        if detail is None:
            return
        chunks = [Chunk(row["chunk_id"], row["text"], row["page_start"], row["page_end"], row["section_path"], row["meta"])
                  for row in sorted(detail["chunks"], key=lambda row: row["chunk_id"])]
        # 批次 16b-Q5：模型调用（耗时）在临界区之外完成；实体对齐与新实体 ID 分配在 save_with 的临界区内，
        # 用保存时的实体重放，多个线程或进程并发抽取不会分到相同的 ID。
        for attempt in range(self._attempts):
            try:
                period = detail["document"].get("period")
                build = self._service.prepare(version, detail["document"]["title"], chunks, anchor=None,
                                              period=period_from_json(period) if period else None)
                break
            except TransientError:
                if attempt + 1 == self._attempts or self._stop.wait(self._retry_delay):
                    raise
        if self._published(version) is None:
            return
        knowledge = self._knowledge.save_with(build, extracted_at=now_iso())
        self._store.append_event(EXTRACTION_DONE, version, {"events": len(knowledge.events), "mentions": len(knowledge.mentions),
                                                            "new_entities": len(knowledge.new_entities)})
        if self._on_extracted is not None:
            try:
                self._on_extracted(version)
            except Exception:
                pass




def china_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def queue_status(tasks: TaskQueue, kind: str, field: str) -> dict[str, Any]:
    """作业状态（ExtractionJobs、LinkJobs 共用）：见 ExtractionJobs.status；field 为 payload 中标识条目的键。"""
    queued = tasks.tasks(kind, statuses={TaskStatus.QUEUED}, limit=1000)
    running = tasks.tasks(kind, statuses={TaskStatus.RUNNING}, limit=1)
    failed = [{field: task.payload[field], "error": task.last_error or "",
               "time": task.finished_at.astimezone(timezone(timedelta(hours=8))).isoformat() if task.finished_at else ""}
              for task in tasks.latest_per(kind, field) if task.status is TaskStatus.FAILED]
    return {"queued": [task.payload[field] for task in queued],
            "running": running[0].payload[field] if running else None,
            "failed": sorted(failed, key=lambda row: row[field]), "idle": not queued and not running}
