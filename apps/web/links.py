"""文档连线数据（引用记录、冲突判断）的文件存储与单线程分析作业。规格：docs/spec-19-links-web.md"""
from __future__ import annotations

import copy
import json
import re
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from typing import Any

from apps.web.knowledge import china_now, queue_status
from apps.web.services import Redactor
from apps.web.store import _write_json, now_iso
from core.app.knowledge import KnowledgeQueries, LinkRepo
from core.app.link_targets import LinkTargets
from core.app.registry import CONFLICTS_JUDGED, LINKS_DONE, DocumentRegistry
from core.app.link import DocumentReferences, LinkService
from core.adapters.memory import InMemoryTaskRepo
from core.app.ports import Chunk, Task, TaskQueue, TaskStatus, TransientError
from core.app.worker import Beat, TaskFailed, Worker
from core.domain.link_targets import RefTargets, TargetPlan, TargetState, apply_plan
from core.domain.relations import ConflictRecord, ReferenceRecord

_VERSION = re.compile(r"v\d+_[0-9a-fA-F]{10}")
CONFLICTS_KEY = "conflicts"


class LinkStore:
    """数据目录下 knowledge/ 子目录中的连线数据：
    - refs/<version_id>.json：{"version_id","extracted_at","report","references":[ReferenceRecord.to_json…]}；
    - conflicts.json：全部已判断的事件对（ConflictRecord.to_json 的列表，按 pair_id 升序），含一致、无关；
    - link_targets.json（批次 18b）：引用目标的物化结果 {"version","docs","targets":[RefTargets.to_json…]}。
    目录或文件不存在时视为空。所有写入都是原子替换（临时文件 + os.replace），读写在同一把锁内。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root) / "knowledge"
        self._lock = RLock()
        self._documents: dict[str, dict[str, Any]] = {}
        for path in sorted((self._root / "refs").glob("*.json")):
            row = json.loads(path.read_text(encoding="utf-8"))
            self._documents[row["version_id"]] = row
        path = self._root / "link_targets.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self._targets = TargetState(dict(data["docs"]), {row["ref_id"]: RefTargets.from_json(row)
                                                             for row in data["targets"]}, data["version"])
        else:
            self._targets = TargetState()
        path = self._root / "conflicts.json"
        self._conflicts = ({row.pair_id: row for row in
                           (ConflictRecord.from_json(data) for data in json.loads(path.read_text(encoding="utf-8")))}
                          if path.exists() else {})

    def extracted(self) -> set[str]:
        """已有引用提取结果的 version_id 集合（结果为空也算）。"""
        with self._lock:
            return set(self._documents)

    def save_references(self, result: DocumentReferences, *, extracted_at: str) -> None:
        """保存一个版本的引用提取结果，已存在则整体替换。version_id 不符合 r"v\\d+_[0-9a-fA-F]{10}" → ValueError，不写任何文件。"""
        with self._lock:
            if _VERSION.fullmatch(result.version_id) is None:
                raise ValueError("文档版本号格式无效")
            document = {"version_id": result.version_id, "extracted_at": extracted_at,
                        "report": result.report.to_json(), "references": [row.to_json() for row in result.references]}
            _write_json(self._root / "refs" / f"{result.version_id}.json", document)
            self._documents[result.version_id] = document

    def references(self) -> list[ReferenceRecord]:
        """全部版本的引用记录，按 ref_id 升序。"""
        with self._lock:
            return sorted((ReferenceRecord.from_json(row) for document in self._documents.values()
                           for row in document["references"]), key=lambda row: row.ref_id)

    def document(self, version_id: str) -> dict[str, Any] | None:
        """某版本保存的完整字典（深拷贝）；没有则 None。"""
        with self._lock:
            return copy.deepcopy(self._documents.get(version_id))

    def conflicts(self) -> list[ConflictRecord]:
        """全部已判断的事件对，按 pair_id 升序。"""
        with self._lock:
            return [self._conflicts[key] for key in sorted(self._conflicts)]

    def judged(self) -> set[str]:
        """已判断的 pair_id 集合。"""
        with self._lock:
            return set(self._conflicts)

    def add_conflict(self, record: ConflictRecord) -> None:
        """保存一条判断结果；同一 pair_id 已存在则替换。"""
        with self._lock:
            values = {**self._conflicts, record.pair_id: record}
            _write_json(self._root / "conflicts.json", [values[key].to_json() for key in sorted(values)])
            self._conflicts = values


    def purge_document(self, version_id: str) -> dict[str, int]:
        with self._lock:
            document = self._documents.pop(version_id, None)
            (self._root / "refs" / f"{version_id}.json").unlink(missing_ok=True)
            kept = {key: row for key, row in self._conflicts.items() if version_id not in (row.version_a, row.version_b)}
            dropped = len(self._conflicts) - len(kept)
            if dropped:
                _write_json(self._root / "conflicts.json", [kept[key].to_json() for key in sorted(kept)])
                self._conflicts = kept
            return {"references": len(document["references"]) if document else 0, "conflicts": dropped}

    def link_targets(self) -> TargetState:
        with self._lock:
            return self._targets          # 不可变：字典只在写入时整体替换，不原地修改

    def link_targets_version(self) -> int:
        with self._lock:
            return self._targets.version

    def sync_link_targets(self, plan: Callable[[TargetState], TargetPlan]) -> TargetState:
        with self._lock:
            change = plan(self._targets)
            if change.empty:
                return self._targets
            state = apply_plan(self._targets, change)
            _write_json(self._root / "link_targets.json",
                        {"version": state.version, "docs": dict(sorted(state.docs.items())),
                         "targets": [state.targets[key].to_json() for key in sorted(state.targets)]})
            self._targets = state
            return state


class LinkJobs:
    """连线作业（批次 15c 起基于任务队列，种类 "links"，payload {"key"}，key 为 "refs:<version_id>" 或 "conflicts"；
    两类条目共用一个种类，按入队顺序执行）。

    批次 20b-r2：引用条目执行时与保存前各核对一次版本仍是 published，否则任务直接完成、不写结果；
    冲突条目每对判断后核对两方仍是 published，否则丢弃该对（不写、不计数，下次仍不是候选）。
    引用条目：分块按 chunk_id 升序交给 service.extract_references(version_id, 标题, chunks)；
    成功后 links.save_references(…, extracted_at=now_iso())，并追加 links_done 事件。
    冲突条目：queries.conflict_candidates(conflict_limit) 逐对 service.judge(a, b, 全部文档标题)，
    每判断完一对立即 links.add_conflict；每对开始前若已请求关闭则直接返回（不算失败）；判断过至少一对时追加 conflicts_judged。
    重试：service 的调用抛出 TransientError 时共尝试 attempts 次，间隔 retry_delay（关闭时可被打断，打断后抛出最后一次异常）；
    其他异常不重试；条目最终失败时任务置 FAILED，错误为 redactor.error(exc)。
    去重：同 key 已在排队中不重复加入；引用条目正在运行时同样不重复，冲突条目例外（见 enqueue_conflicts）。
    run_workers 为 False 时只入队、不在本进程执行。"""

    KIND = "links"

    def __init__(self, store: DocumentRegistry, queries: KnowledgeQueries, links: LinkRepo, service: LinkService,
                 redactor: Redactor, *, retry_delay: float = 5.0, attempts: int = 3, conflict_limit: int = 100,
                 tasks: TaskQueue | None = None, run_workers: bool = True, worker_id: str = "web",
                 poll_seconds: float = 1.0, targets: LinkTargets | None = None) -> None:
        """queries：有效视图查询（批次 15b 起取代原 knowledge 参数），冲突候选取自 queries.conflict_candidates。
        targets（批次 18b）：引用提取保存后立即同步物化的匹配（缺省为 LinkTargets(links)）；同步失败不影响作业结果，
        读取视图时会再同步。"""
        self._store, self._queries, self._links = store, queries, links
        self._targets = targets if targets is not None else LinkTargets(links)
        self._service, self._redactor = service, redactor
        self._retry_delay, self._attempts, self._conflict_limit = retry_delay, attempts, conflict_limit
        self._lock = RLock()
        self._stop = threading.Event()
        self._tasks = tasks if tasks is not None else InMemoryTaskRepo(china_now)
        self._run_workers = run_workers
        self.worker = Worker(self._tasks, {self.KIND: self.handle}, worker_id=f"{worker_id}-links",
                             lease_seconds=3600, poll_seconds=poll_seconds, redact=redactor.error)

    def start(self) -> None:
        if self._run_workers:
            self.worker.start()

    def close(self) -> None:
        """停止：不再开始新的条目，等待当前条目结束（重试等待可被打断）后返回。"""
        self._stop.set()
        self.worker.close()

    def _active(self, statuses: set[TaskStatus]) -> set[str]:
        return {task.payload.get("key") for task in self._tasks.tasks(self.KIND, statuses=statuses, limit=100_000)}

    def _enqueue(self, key: str) -> None:
        self._tasks.enqueue(f"{self.KIND}:{key}:{uuid.uuid4().hex}", self.KIND, {"key": key})
        self.worker.notify()

    def enqueue_references(self, version_id: str, *, force: bool = False) -> bool:
        """加入引用提取队列，返回是否真的加入。
        - 版本不存在 → KeyError；存在但不是 published → ValueError。
        - 已在队列中或正在运行 → False；已有结果且 force 为 False → False。
        """
        with self._lock:
            document = self._store.detail(version_id)
            if document is None:
                raise KeyError(version_id)
            if document["document"]["status"] != "published":
                raise ValueError("只能分析已发布文档")
            key = f"refs:{version_id}"
            if key in self._active({TaskStatus.QUEUED, TaskStatus.RUNNING}) or (
                    not force and version_id in self._links.extracted()):
                return False
            self._enqueue(key)
            return True

    def enqueue_missing(self) -> int:
        """把所有 published、尚无引用提取结果、且不在队列中/未在运行的版本按 version_id 升序加入队列，返回加入数量。"""
        with self._lock:
            return sum(self.enqueue_references(row["version_id"]) for row in
                       sorted(self._store.documents(), key=lambda row: row["version_id"]) if row["status"] == "published")

    def enqueue_conflicts(self) -> bool:
        """加入一次冲突判断；排队中已有尚未开始的冲突判断 → False（正在运行的不算，因为运行期间可能又有新事件）。"""
        with self._lock:
            if CONFLICTS_KEY in self._active({TaskStatus.QUEUED}):
                return False
            self._enqueue(CONFLICTS_KEY)
            return True

    def on_extracted(self, version_id: str) -> None:
        """某份文档的实体与事件抽取完成后的回调：加入该文档的引用提取，并加入一次冲突判断；任何异常都忽略。"""
        try:
            self.enqueue_references(version_id)
            self.enqueue_conflicts()
        except Exception:
            pass

    def candidates(self) -> int:
        """尚未判断的候选事件对数量（只看已发布文档的事件，最多数到 CANDIDATE_COUNT_CAP）。"""
        return self._queries.conflict_candidate_count()

    def status(self) -> dict[str, Any]:
        """{"queued": [key…按入队顺序，最多 1000 个], "running": key 或 None,
            "failed": [{"key","error","time"}…按 key 升序], "idle"}。语义同 ExtractionJobs.status。"""
        return queue_status(self._tasks, self.KIND, "key")

    def handle(self, task: Task, beat: Beat) -> None:
        try:
            self._run(task.payload["key"])
        except Exception as exc:
            raise TaskFailed(self._redactor.error(exc)) from exc

    def _published(self, version: str) -> dict[str, Any] | None:
        detail = self._store.detail(version)
        return detail if detail is not None and detail["document"]["status"] == "published" else None

    def _retry(self, call: Any) -> Any:
        for attempt in range(self._attempts):
            try:
                return call()
            except TransientError:
                if attempt + 1 == self._attempts or self._stop.wait(self._retry_delay):
                    raise

    def _run(self, key: str) -> None:
        if key == CONFLICTS_KEY:
            titles = {row["version_id"]: row["title"] for row in self._store.documents()}
            pairs = self._queries.conflict_candidates(self._conflict_limit)
            judged = 0
            try:
                for a, b in pairs:
                    if self._stop.is_set():
                        return
                    record = self._retry(lambda: self._service.judge(a, b, titles))
                    if not (self._published(record.version_a) and self._published(record.version_b)):
                        continue                # 批次 20b-r2：判断期间一方已撤下或删除 → 丢弃
                    self._links.add_conflict(record)
                    judged += 1
            finally:
                if judged:
                    self._store.append_event(CONFLICTS_JUDGED, None, {"judged": judged})
            return
        version = key.removeprefix("refs:")
        detail = self._published(version)
        if detail is None:
            return                              # 批次 20b-r2：已不存在或不再是已发布 → 任务直接完成，不写结果
        chunks = [Chunk(row["chunk_id"], row["text"], row["page_start"], row["page_end"], row["section_path"], row["meta"])
                  for row in sorted(detail["chunks"], key=lambda row: row["chunk_id"])]
        result = self._retry(lambda: self._service.extract_references(version, detail["document"]["title"], chunks))
        if self._published(version) is None:
            return                              # 提取期间被撤下或删除 → 丢弃结果
        self._links.save_references(result, extracted_at=now_iso())
        self._store.append_event(LINKS_DONE, version, {"references": len(result.references)})
        try:                                    # 批次 18b：提取后物化；失败时由读取视图时的同步补上
            self._targets.current({row["version_id"]: row["title"] for row in self._store.documents()
                                   if row["status"] == "published"}, self._links.references())
        except Exception:
            pass
