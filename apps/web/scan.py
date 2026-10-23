"""服务器目录增量扫描：扫描根配置、JSON 扫描索引、扫描作业（批次 15d）。规格见 docs/spec-27-folder-import.md 第三节。"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Any

from apps.web.jobs import DuplicateUpload, JobManager, upload_name_error
from apps.web.knowledge import china_now
from apps.web.services import Redactor
from apps.web.store import _write_json
from core.adapters.docling_http import SUPPORTED_EXTENSIONS
from core.adapters.local_dir import LocalDirectory
from core.adapters.memory import InMemoryTaskRepo
from core.app.ports import PermanentError, Task, TaskQueue, TaskStatus, TransientError
from core.app.lifecycle import LifecycleService
from core.app.scan import ScanIndex, ScanInterrupted, ScanService, Submission
from core.app.worker import Beat, TaskFailed, Worker
from core.domain.folders import normalize_folder_path
from core.domain.scan import DUPLICATE, NEW, REJECTED, ScanEntry, join_folder, split_import_path

SCAN = "scan"
MIN_INTERVAL_MINUTES = 5


def parse_scan_roots(text: str | None, *, variable: str = "HAINA_SCAN_ROOTS") -> dict[str, Path]:
    """解析环境变量 HAINA_SCAN_ROOTS：JSON 对象 {名称: 绝对目录路径}。空串或 None → {}。
    variable：错误消息中的变量名（批次 17a 的 HAINA_MAIL_ROOTS 也用本函数解析）。
    名称必须是单段的规范文件夹名（normalize_folder_path(名称) == 名称，且不含 "/"），作为文档文件夹的第一段；
    路径必须是已存在的绝对目录。不是 JSON 对象、名称或路径不合规 → ValueError（消息不含路径以外的内容）。
    返回 {名称: Path}，按名称排序。"""
    if not text or not text.strip():
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{variable} 必须是 JSON 对象") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{variable} 必须是 JSON 对象")
    roots: dict[str, Path] = {}
    for name, raw in sorted(value.items()):
        try:
            valid = isinstance(name, str) and bool(name) and normalize_folder_path(name) == name and "/" not in name
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f"扫描根名称无效：{name!r}")
        path = Path(raw) if isinstance(raw, str) else None
        if path is None or not path.is_absolute() or not path.is_dir():
            raise ValueError(f"扫描根 {name} 的目录不存在或不是绝对路径")
        roots[name] = path
    return roots


def parse_scan_interval(text: str | None, *, variable: str = "HAINA_SCAN_INTERVAL_MINUTES") -> int | None:
    """HAINA_SCAN_INTERVAL_MINUTES：空 → None（不定时扫描）；否则为不小于 MIN_INTERVAL_MINUTES 的整数，不合规 → ValueError。"""
    if not text or not text.strip():
        return None
    try:
        value = int(text.strip())
    except ValueError as exc:
        raise ValueError(f"{variable} 必须是整数") from exc
    if value < MIN_INTERVAL_MINUTES:
        raise ValueError(f"定时扫描间隔不能小于 {MIN_INTERVAL_MINUTES} 分钟")
    return value


class JsonScanIndex:
    """ScanIndex 的 JSON 文件实现（JSON 模式使用）：<root>/scan/files/<名称哈希>.json 保存
    {"root", "entries": {path: ScanEntry 字段}}；<root>/scan/runs/<scan_id>.json 保存报告。原子写入，进程内加锁。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root) / "scan"
        self._lock = RLock()

    def _file(self, root: str) -> Path:
        return self._root / "files" / (hashlib.sha256(root.encode("utf-8")).hexdigest()[:24] + ".json")

    def _load(self, root: str) -> dict[str, dict[str, Any]]:
        path = self._file(root)
        return json.loads(path.read_text(encoding="utf-8"))["entries"] if path.exists() else {}

    def entries(self, root: str) -> dict[str, ScanEntry]:
        with self._lock:
            return {path: ScanEntry(**row) for path, row in self._load(root).items()}

    def put(self, root: str, entries: Sequence[ScanEntry]) -> None:
        if not entries:
            return
        with self._lock:
            rows = self._load(root)
            rows.update((entry.path, asdict(entry)) for entry in entries)
            _write_json(self._file(root), {"root": root, "entries": dict(sorted(rows.items()))})

    def remove(self, root: str, paths: Collection[str]) -> None:
        with self._lock:
            rows = self._load(root)
            if any(path in rows for path in paths):
                for path in paths:
                    rows.pop(path, None)
                _write_json(self._file(root), {"root": root, "entries": rows})

    def save_run(self, report: dict[str, Any]) -> None:
        if any(char in report["scan_id"] for char in "/\\.") or not report["scan_id"]:
            raise ValueError("scan_id 无效")
        with self._lock:
            _write_json(self._root / "runs" / f"{report['scan_id']}.json", report)

    def _all_runs(self) -> list[dict[str, Any]]:
        folder = self._root / "runs"
        return [json.loads(path.read_text(encoding="utf-8")) for path in folder.glob("*.json")] if folder.exists() else []

    def runs(self, root: str, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit 必须为正数")
        with self._lock:
            rows = sorted((row for row in self._all_runs() if row["root"] == root), key=lambda row: row["scan_id"])
            rows.sort(key=lambda row: row["started_at"], reverse=True)
            return rows[:limit]

    def run(self, scan_id: str) -> dict[str, Any] | None:
        if not scan_id or any(char in scan_id for char in "/\\.") :
            return None
        with self._lock:
            path = self._root / "runs" / f"{scan_id}.json"
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


class ScanBusy(Exception):
    """同一扫描根已有排队或执行中的扫描。task_id：那个任务。"""

    def __init__(self, task_id: str) -> None:
        super().__init__("该目录正在扫描")
        self.task_id = task_id


class ScanJobs:
    """扫描作业（任务种类 "scan"，payload {"root", "scan_id"}，幂等键 f"scan:{scan_id}"，租约 1800 秒并按进度续租）。

    - enqueue(root)：根不在配置中 → KeyError；该根已有排队或执行中的扫描 → ScanBusy；否则入队并返回任务 ID。
      scan_id 为 f"{china_now():%Y%m%d%H%M%S}-{uuid4().hex[:8]}"。
    - handle：用 ScanService 扫描 LocalDirectory(根目录)，提交导入时文件夹为 join_folder(根名称, 相对文件夹)，
      origin 为 "scan"。进度经 beat 报告。close() 期间被要求停止 → 抛 TransientError（任务回到队列，已处理部分不再重复）。
      根目录不存在等 PermanentError → TaskFailed(脱敏消息)。
    - status()：{"roots": [{"name", "path", "active": {"task_id","status","progress","scan_id"} 或 None,
      "last_run": 最近一份报告去掉 "items" 后的字典 或 None}…按名称], "interval_minutes"}。
    - runs(root, limit) / run(scan_id)：转发给索引（runs 的根不在配置中 → KeyError）。
    - start_schedule(interval_minutes)：后台线程每隔 interval 分钟对所有根 enqueue 一次（忙的根跳过，异常忽略）；
      第一次在 interval 之后。close() 停止它。
    - run_workers 为 False 时只入队、不在本进程执行。"""

    KIND = SCAN

    def __init__(self, roots: Mapping[str, Path], index: ScanIndex, jobs: JobManager, redactor: Redactor, *,
                 max_bytes: int = 200_000_000, tasks: TaskQueue | None = None, run_workers: bool = True,
                 worker_id: str = "web", poll_seconds: float = 1.0,
                 tree_factory: Callable[[Path], Any] = LocalDirectory, minute_seconds: float = 60.0,
                 lifecycle: LifecycleService | None = None) -> None:
        """minute_seconds：定时扫描里“一分钟”的秒数，仅测试使用。
        lifecycle（批次 20a）：给出时，源文件缺失的版本自动撤下（条目带 "withdrawn"），因缺失而撤下的版本在文件以相同内容
        再出现时自动恢复（条目带 "restored"）；恢复失败时该文件记为 FAILED，下次扫描重试。"""
        self._lifecycle = lifecycle
        self._roots = dict(roots)
        self._index, self._jobs, self._redactor = index, jobs, redactor
        self._max_bytes, self._tree_factory, self._minute = max_bytes, tree_factory, minute_seconds
        self._tasks = tasks if tasks is not None else InMemoryTaskRepo(china_now)
        self._run_workers = run_workers
        self._stop = threading.Event()
        self._lock = RLock()
        self._interval: int | None = None
        self._schedule: threading.Thread | None = None
        self.worker = Worker(self._tasks, {self.KIND: self.handle}, worker_id=f"{worker_id}-scan", lease_seconds=1800,
                             poll_seconds=poll_seconds, redact=redactor.error)

    @property
    def roots(self) -> dict[str, Path]:
        return dict(self._roots)

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

    def enqueue(self, root: str) -> str:
        if root not in self._roots:
            raise KeyError(root)
        with self._lock:
            active = self._active(root)
            if active is not None:
                raise ScanBusy(active.task_id)
            scan_id = f"{china_now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"
            task = self._tasks.enqueue(f"{self.KIND}:{scan_id}", self.KIND, {"root": root, "scan_id": scan_id})
        self.worker.notify()
        return task.task_id

    def _submit(self, root: str) -> Callable[[str, bytes, str], Submission]:
        def submit(path: str, data: bytes, sha256: str) -> Submission:
            try:
                folder, filename = split_import_path(path)
                problem = upload_name_error(filename)
                if problem is not None:
                    return Submission(REJECTED, error=problem)
                job_id = self._jobs.submit(filename, data, folder=join_folder(root, folder), origin="scan")
            except DuplicateUpload as exc:
                restored = (self._lifecycle is not None and exc.job_id is None
                            and self._lifecycle.restore_reappeared(exc.version_id))
                return Submission(DUPLICATE, version_id=exc.version_id, job_id=exc.job_id, restored=restored)
            except (ValueError, PermanentError) as exc:
                return Submission(REJECTED, error=self._redactor.error(exc))
            job = self._jobs.get(job_id)
            return Submission(NEW, version_id=job["version_id"] if job else None, job_id=job_id)
        return submit

    def handle(self, task: Task, beat: Beat) -> None:
        root, scan_id = task.payload["root"], task.payload["scan_id"]
        if root not in self._roots:
            raise TaskFailed("扫描根已不在配置中")
        on_missing = (None if self._lifecycle is None else
                      lambda version: {"withdrawn": self._lifecycle.withdraw_missing(version)})
        service = ScanService(self._index, self._submit(root), extensions=SUPPORTED_EXTENSIONS,
                              max_bytes=self._max_bytes, clock=china_now, on_missing=on_missing)
        try:
            tree = self._tree_factory(self._roots[root])
            service.run(root, tree, scan_id=scan_id, progress=lambda text: beat(text),
                        should_stop=self._stop.is_set)
        except ScanInterrupted as exc:
            raise TransientError("扫描被中断，稍后继续") from exc
        except PermanentError as exc:
            raise TaskFailed(self._redactor.error(exc)) from exc

    def status(self) -> dict[str, Any]:
        rows = []
        for name, path in sorted(self._roots.items()):
            active = self._active(name)
            last = self._index.runs(name, 1)
            summary = {key: value for key, value in last[0].items() if key != "items"} if last else None
            rows.append({"name": name, "path": str(path), "last_run": summary,
                         "active": None if active is None else {
                             "task_id": active.task_id, "status": active.status.value, "progress": active.progress,
                             "scan_id": active.payload["scan_id"]}})
        return {"roots": rows, "interval_minutes": self._interval}

    def runs(self, root: str, limit: int = 20) -> list[dict[str, Any]]:
        if root not in self._roots:
            raise KeyError(root)
        return [{key: value for key, value in row.items() if key != "items"} for row in self._index.runs(root, limit)]

    def run(self, scan_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self._index.run(scan_id))

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

        self._schedule = threading.Thread(target=loop, name="haina-scan-schedule", daemon=True)
        self._schedule.start()
