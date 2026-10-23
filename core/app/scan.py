"""服务器目录增量扫描用例（批次 15d）。规格见 docs/spec-27-folder-import.md。"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from core.domain.scan import (CHANGED, DUPLICATE, FAILED, MISSING, NEW, OUTCOMES, REJECTED, SKIPPED, UNCHANGED,
                              FileStat, ScanEntry, plan_scan)

ITEM_LIMIT = 2000           # 报告里最多列出的条目数（unchanged 不列出）
BATCH = 100                 # 每处理这么多个文件写一次索引并报告一次进度


@runtime_checkable
class SourceTree(Protocol):
    """一个扫描根目录。"""

    def files(self) -> Iterable[FileStat]:
        """根下全部普通文件（递归；不跟随符号链接，符号链接本身也不列出）。path 为相对根的 "/" 分隔 NFC 路径。
        根目录不存在或不可读 → PermanentError。子目录不可读时跳过该子目录。"""

    def read(self, path: str) -> bytes:
        """读取 files() 给出的某个文件。path 指向根之外（含 ".."、绝对路径、经符号链接）→ PermanentError；
        文件已不存在或读取失败 → OSError（含 FileNotFoundError）。"""


@runtime_checkable
class ScanIndex(Protocol):
    """扫描索引与扫描报告。root 为扫描根的名称。"""

    def entries(self, root: str) -> dict[str, ScanEntry]:
        """该根的全部条目，键为 path。没有为 {}。返回值是副本。"""

    def put(self, root: str, entries: Sequence[ScanEntry]) -> None:
        """按 path 新增或整体替换条目。"""

    def remove(self, root: str, paths: Collection[str]) -> None:
        """删除这些条目；不存在的忽略。"""

    def save_run(self, report: dict[str, Any]) -> None:
        """保存（或按 scan_id 整体替换）一份扫描报告（ScanService.run 的返回值）。"""

    def runs(self, root: str, limit: int = 20) -> list[dict[str, Any]]:
        """该根最近的报告，按 started_at 从新到旧，最多 limit 份；limit < 1 → ValueError。返回值是副本。"""

    def run(self, scan_id: str) -> dict[str, Any] | None:
        """按 scan_id 取报告；没有为 None。"""


@dataclass(frozen=True)
class Submission:
    """提交一个文件导入的结果。outcome：NEW（已入队）、DUPLICATE（内容与已登记文档或进行中的导入相同）、REJECTED。
    version_id：NEW 为新版本号，DUPLICATE 为已有版本号；job_id：NEW 为作业 ID，DUPLICATE 时可能有；error：REJECTED 的原因。"""
    outcome: str
    version_id: str | None = None
    job_id: str | None = None
    error: str | None = None
    restored: bool = False          # 批次 20a：DUPLICATE 时，该版本因源文件缺失被撤下、本次已恢复


Submit = Callable[[str, bytes, str], Submission]      # submit(相对路径, 内容, sha256) → Submission


class ScanInterrupted(Exception):
    """扫描被要求停止。已处理部分的索引已写入，重新扫描时会跳过它们。"""


class ScanService:
    """run(root, tree, *, scan_id, started_at, progress, should_stop) 执行一次增量扫描并返回报告：

    {"scan_id", "root", "started_at", "finished_at"（clock() 的 isoformat）,
     "counts": {OUTCOMES 中每一项: 数量},
     "items": [{"path", "outcome", 以及按情况的 "version_id"、"previous_version"、"job_id"、"error"、"reason"}…],
     "truncated": 条目是否超过 ITEM_LIMIT 被截断}

    处理规则（按 plan_scan 的结果，to_read 按 path 升序逐个处理）：
    - unchanged → UNCHANGED（不读取，计数，不列入 items）；skipped → SKIPPED（items 带 "reason"）；
    - to_read：tree.read 抛异常 → FAILED（"error" 为 f"{类型名}: {消息}"，不进索引）；
      计算 sha256，若索引中有该 path 且 sha256 相同 → UNCHANGED（只更新索引中的 size、mtime_ns，不列入 items）；
      否则调用 submit(path, data, sha256)：
        NEW → 索引中原来没有该 path 记为 NEW，原来有记为 CHANGED（items 带 "previous_version"）；
        DUPLICATE → DUPLICATE；REJECTED → REJECTED（"error"）。三者都写入索引（outcome、version_id 取自上面）。
      submit 抛异常或返回未知的 outcome → FAILED，不进索引。
    - missing → MISSING，并从索引中删除（文件再出现时按新文件处理）。批次 20a：删除后，若该条目的 version_id 不再被
      本根中任何其他路径引用（文件被移动、或另有同内容的文件时仍被引用），且给出了 on_missing，则把 on_missing(version_id)
      的结果并入该条目；on_missing 抛异常时条目带 "error"，扫描继续。
    - 批次 20a：DUPLICATE 的 Submission.restored 为真时条目带 "restored": True。
    - items 顺序：处理顺序（skipped 先于 to_read 的结果，最后是 missing）；超过 ITEM_LIMIT 只保留前 ITEM_LIMIT 条。
    - 每处理 BATCH 个 to_read 文件写一次索引并调用 progress(f"{已处理}/{总数}")；开始时调用 progress(f"0/{总数}")。
    - 每个 to_read 文件处理前检查 should_stop()：为真时先写入已处理部分的索引，再抛 ScanInterrupted（不保存报告、不删除 missing）。
    - 全部完成后删除 missing 条目，index.save_run(报告)，返回报告。
    tree.files() 抛出的异常原样抛出（不保存报告）。"""

    def __init__(self, index: ScanIndex, submit: Submit, *, extensions: Collection[str], max_bytes: int,
                 clock: Callable[[], datetime],
                 on_missing: Callable[[str], dict[str, Any]] | None = None) -> None:
        """on_missing（批次 20a）：on_missing(version_id) → 并入该缺失条目的字段（如 {"withdrawn": True}）。"""
        self._index, self._submit = index, submit
        self._extensions, self._max_bytes, self._clock = frozenset(extensions), max_bytes, clock
        self._on_missing = on_missing

    def run(self, root: str, tree: SourceTree, *, scan_id: str, started_at: str | None = None,
            progress: Callable[[str], Any] = lambda text: None,
            should_stop: Callable[[], bool] = lambda: False) -> dict[str, Any]:
        started = started_at or self._clock().isoformat()
        previous = self._index.entries(root)
        plan = plan_scan(previous, list(tree.files()), extensions=self._extensions, max_bytes=self._max_bytes)
        counts = dict.fromkeys(OUTCOMES, 0)
        items: list[dict[str, Any]] = []
        counts[UNCHANGED] += len(plan.unchanged)
        for path, reason in plan.skipped:
            counts[SKIPPED] += 1
            items.append({"path": path, "outcome": SKIPPED, "reason": reason})
        pending: list[ScanEntry] = []
        total = len(plan.to_read)
        progress(f"0/{total}")
        for number, stat in enumerate(plan.to_read, 1):
            if should_stop():
                if pending:
                    self._index.put(root, pending)
                raise ScanInterrupted("扫描已停止")
            item = self._one(tree, stat, previous.get(stat.path), pending)
            counts[item["outcome"]] += 1
            if item["outcome"] != UNCHANGED:
                items.append(item)
            if number % BATCH == 0:
                self._index.put(root, pending)
                pending = []
                progress(f"{number}/{total}")
        if pending:
            self._index.put(root, pending)
        if total % BATCH:
            progress(f"{total}/{total}")
        if plan.missing:
            self._index.remove(root, plan.missing)
        alive = {entry.version_id for entry in self._index.entries(root).values()} if plan.missing else set()
        for path in plan.missing:
            counts[MISSING] += 1
            version = previous[path].version_id
            item = {"path": path, "outcome": MISSING, "version_id": version}
            if self._on_missing is not None and version is not None and version not in alive:
                try:
                    item.update(self._on_missing(version))
                except Exception as exc:
                    item["error"] = f"{type(exc).__name__}: {exc}"
            items.append(item)
        report = {"scan_id": scan_id, "root": root, "started_at": started, "finished_at": self._clock().isoformat(),
                  "counts": counts, "items": items[:ITEM_LIMIT], "truncated": len(items) > ITEM_LIMIT}
        self._index.save_run(report)
        return report

    def _one(self, tree: SourceTree, stat: FileStat, entry: ScanEntry | None, pending: list[ScanEntry]) -> dict[str, Any]:
        try:
            data = tree.read(stat.path)
        except Exception as exc:
            return {"path": stat.path, "outcome": FAILED, "error": f"{type(exc).__name__}: {exc}"}
        digest = hashlib.sha256(data).hexdigest()
        if entry is not None and entry.sha256 == digest:
            pending.append(ScanEntry(stat.path, stat.size, stat.mtime_ns, digest, entry.version_id, entry.outcome))
            return {"path": stat.path, "outcome": UNCHANGED}
        try:
            result = self._submit(stat.path, data, digest)
        except Exception as exc:
            return {"path": stat.path, "outcome": FAILED, "error": f"{type(exc).__name__}: {exc}"}
        item: dict[str, Any] = {"path": stat.path}
        if result.outcome == NEW:
            outcome = NEW if entry is None else CHANGED
            item.update(outcome=outcome, version_id=result.version_id, job_id=result.job_id)
            if entry is not None:
                item["previous_version"] = entry.version_id
        elif result.outcome == DUPLICATE:
            outcome = DUPLICATE
            item.update(outcome=outcome, version_id=result.version_id)
            if result.job_id is not None:
                item["job_id"] = result.job_id
            if result.restored:
                item["restored"] = True
        elif result.outcome == REJECTED:
            outcome = REJECTED
            item.update(outcome=outcome, error=result.error or "")
        else:
            return {"path": stat.path, "outcome": FAILED, "error": f"ValueError: 未知的提交结果 {result.outcome}"}
        pending.append(ScanEntry(stat.path, stat.size, stat.mtime_ns, digest,
                                 None if outcome == REJECTED else result.version_id, outcome))
        return item
