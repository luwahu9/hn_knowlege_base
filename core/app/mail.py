"""邮件：端口与用例（批次 17a）。规格见 docs/spec-30-mail.md。

- MailIndex：检索索引（Elasticsearch；测试用内存实现）。
- MailCatalog：扫描目录（PostgreSQL）：每个目录与每个文件的最近状态，以及扫描报告。
- MailTree：一个邮件根目录（本地目录）。
- MailScanService：按目录增量扫描。目录的修改时间没变时不列出其中的文件，数亿封邮件的日常扫描只需逐个查看目录。
"""
from __future__ import annotations

import copy
from collections.abc import Callable, Collection, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from core.app.ports import PermanentError
from core.domain.folders import MAX_DEPTH, normalize_folder_path
from core.domain.mail import (MAIL_EXTENSIONS, MAX_MAIL_BYTES, Address, MailDoc, NotMail, build_doc, make_snippet,
                              parse_eml)
from core.domain.mail_qa import ThreadKey
from core.domain.mail_query import MailQuery
from core.domain.permissions import FolderScope
from core.domain.scan import HIDDEN, INVALID_PATH, FileStat, join_folder, skip_reason

BULK = 500                  # 每积累这么多条写一次检索索引与扫描目录
ITEM_LIMIT = 2000           # 报告里最多列出的条目数
PROGRESS_DIRS = 50          # 每处理这么多个目录报告一次进度
RECIPIENTS_SHOWN = 5        # 检索结果里每封邮件列出的收件人数
RELEVANT_LIMIT_MAX = 50     # 相关度检索的候选上限（批次 17b）
THREAD_LIMIT_MAX = 500      # 一次取出的会话线索记录上限（批次 17b）


# ---------------------------------------------------------------- 检索索引

@dataclass(frozen=True)
class MailHit:
    """检索结果中的一封邮件。to：To 中前 RECIPIENTS_SHOWN 个；recipients：To、Cc、Bcc 的地址总数；
    snippet：mail.make_snippet 规则下的正文摘要（正文的连续子串，不超过 SNIPPET_CHARS）；attachments：附件个数；
    message_id（批次 17b）：用于会话线索去重。"""
    mail_id: str
    folder: str
    subject: str
    sender: Address | None
    to: tuple[Address, ...]
    recipients: int
    sent_at: str | None
    snippet: str
    attachments: int
    size: int
    message_id: str | None = None


def hit_from_doc(doc: MailDoc, terms: Sequence[str] = ()) -> MailHit:
    """由完整记录生成摘要行（内存实现与会话线索使用）：to 取前 RECIPIENTS_SHOWN 个，snippet = make_snippet(body, terms)。"""
    return MailHit(doc.mail_id, doc.folder, doc.subject, doc.sender, doc.to[:RECIPIENTS_SHOWN],
                   len(doc.to) + len(doc.cc) + len(doc.bcc), doc.sent_at, make_snippet(doc.body, terms),
                   len(doc.attachments), doc.size, doc.message_id)


@dataclass(frozen=True)
class MailPage:
    """rows：本页；total：满足条件的邮件数，超过 mail_query.TOTAL_CAP 时为 TOTAL_CAP 且 total_exact 为 False。"""
    rows: tuple[MailHit, ...]
    total: int
    total_exact: bool


@runtime_checkable
class MailIndex(Protocol):
    """邮件检索索引。写入后调用 refresh() 才保证 search 能看到；get 不需要 refresh。"""

    def ensure(self) -> None:
        """索引不存在时按规格的映射创建；已存在且映射版本相同时什么都不做；版本不同 → PermanentError。幂等。"""

    def upsert(self, docs: Sequence[MailDoc]) -> None:
        """按 mail_id 新增或整体替换。docs 为空时什么都不做。连接失败、超时、5xx → TransientError；
        任一条被拒绝（4xx、映射错误）→ PermanentError，消息含第一个失败的 mail_id 与原因。"""

    def delete(self, mail_ids: Collection[str]) -> None:
        """删除这些记录；不存在的忽略。"""

    def get(self, mail_id: str) -> MailDoc | None: ...

    def search(self, query: MailQuery, *, scope: FolderScope | None, offset: int, limit: int) -> MailPage:
        """满足 query 且（scope 为 None，或 scope.contains(folder)）的记录，按 mail_query.result_order 排序后取
        [offset, offset + limit)。匹配语义以 mail_query.mail_matches 为准。分页参数先经 mail_query.check_page 检查（ValueError）。"""

    def relevant(self, text: str, query: MailQuery, *, scope: FolderScope | None, limit: int) -> list[MailDoc]:
        """相关度检索（批次 17b，小安问邮件的候选）：满足 query 与 scope、且检索文本（mail_qa.doc_texts 的字段）中
        至少出现一个 mail_qa.question_keys(text) 词元的记录，按相关度从高到低取前 limit 条。
        - limit 取 1–RELEVANT_LIMIT_MAX，否则 ValueError；text 没有词元 → []（不发请求）。
        - 相关度的具体算法由实现决定（内存实现：mail_qa.relevance；ES：BM25）。契约只约定：含全部问题词元的记录
          排在只含其中一个的记录之前（两者其余条件相近时）；相关度相同时按 result_order。"""

    def thread(self, key: ThreadKey, *, scope: FolderScope | None, limit: int) -> list[MailHit]:
        """会话线索（批次 17b）：满足 mail_qa.in_thread(doc, key) 且在 scope 内的记录，按发送时间从早到晚
        （没有发送时间的排在最后，同一时间按 mail_id 升序），最多 limit 条（1–THREAD_LIMIT_MAX，否则 ValueError）。
        MailHit.snippet 为正文开头（不超过 SNIPPET_CHARS）。同一封邮件的多个副本都返回，由调用方去重。"""

    def count(self) -> int:
        """记录总数（refresh 之后准确）。"""

    def refresh(self) -> None: ...


# ---------------------------------------------------------------- 扫描目录

@dataclass(frozen=True)
class DirRecord:
    """一个已扫描目录。dir：相对邮件根的规范路径（根为 ""）；mtime_ns：列出时的目录修改时间，
    为 -1 表示该目录有文件读取失败、下次必须重新列出；children：子目录名（升序）；mails：本目录直接包含、已入索引的邮件数。"""
    dir: str
    mtime_ns: int
    children: tuple[str, ...]
    mails: int


@dataclass(frozen=True)
class FileRecord:
    """一个已处理的文件。mail_id 为 None 表示内容不是邮件（NotMail），未入索引。"""
    name: str
    size: int
    mtime_ns: int
    mail_id: str | None


@runtime_checkable
class MailCatalog(Protocol):
    """扫描目录。root 为邮件根的名称。返回值都是副本。"""

    def dir(self, root: str, dir: str) -> DirRecord | None: ...

    def dirs(self, root: str, dirs: Collection[str]) -> dict[str, DirRecord]:
        """批量取目录记录（批次 17a 修正）：{dir: DirRecord}，只含存在的；dirs 为空 → {}。
        PG 实现为一条按主键查找的语句（dir = ANY(…)），不得逐个查询。"""

    def put_dir(self, root: str, record: DirRecord) -> None:
        """新增或整体替换。"""

    def files(self, root: str, dir: str) -> dict[str, FileRecord]:
        """该目录直接包含的文件记录，键为文件名；没有为 {}。"""

    def put_files(self, root: str, dir: str, records: Sequence[FileRecord]) -> None:
        """按文件名新增或整体替换。"""

    def remove_files(self, root: str, dir: str, names: Collection[str]) -> None: ...

    def tree_mail_ids(self, root: str, dir: str, *, batch: int = BULK) -> Iterator[list[str]]:
        """dir 及其全部子孙目录中、mail_id 非空的文件的 mail_id，分批给出（每批不超过 batch 个）。
        dir 为 "" 时是整个根。生成过程中不得修改这些记录。"""

    def remove_tree(self, root: str, dir: str) -> int:
        """删除 dir 及其全部子孙目录的目录记录与文件记录，返回删除的目录记录数。dir 为 "" 时是整个根。"""

    def folder_counts(self, *, depth: int) -> dict[str, int]:
        """各邮件根下的邮件数：键为 join_folder(根名称, 目录) 截取前 depth 段（depth ≥ 1，否则 ValueError），
        值为位于该文件夹之内（含子孙）的 DirRecord.mails 之和。只列出值大于 0 的。"""

    def stats(self, root: str) -> dict[str, int]:
        """{"dirs": 目录记录数, "mails": 各目录记录的 mails 之和}。只读目录记录（文件记录可达数亿行，不在此统计）。"""

    def save_run(self, report: dict[str, Any]) -> None:
        """保存（或按 scan_id 整体替换）一份扫描报告（MailScanService.run 的返回值，或分片扫描的计划报告）。"""

    def runs(self, root: str, limit: int = 20) -> list[dict[str, Any]]:
        """该根最近的报告，按 started_at 从新到旧（同时刻按 scan_id 降序），最多 limit 份；limit < 1 → ValueError。
        批次 17b：不含分片报告（带 "parent" 键的）。"""

    def parts(self, scan_id: str) -> list[dict[str, Any]]:
        """批次 17b：report["parent"] == scan_id 的分片报告，按 scan_id 升序。"""

    def run(self, scan_id: str) -> dict[str, Any] | None: ...


# ---------------------------------------------------------------- 邮件根目录

@dataclass(frozen=True)
class DirListing:
    """目录的直接内容。files 中 FileStat.path 只是文件名；dirs 为子目录名。名称都已 NFC 规范化，不含符号链接与重解析点。"""
    files: tuple[FileStat, ...]
    dirs: tuple[str, ...]


@runtime_checkable
class MailTree(Protocol):
    """dir 为相对根的 "/" 分隔路径（根为 ""）。"""

    def mtime(self, dir: str) -> int | None:
        """目录的修改时间（纳秒）；不存在或不是目录 → None。"""

    def list(self, dir: str) -> DirListing:
        """列出目录；不可读 → OSError。"""

    def read(self, dir: str, name: str) -> bytes:
        """读取 list 给出的文件；路径越出根 → PermanentError；读取失败 → OSError。"""


# ---------------------------------------------------------------- 扫描

COUNTS = ("dirs_listed", "dirs_unchanged", "dirs_removed", "dirs_failed", "dirs_skipped",
          "new", "changed", "unchanged", "removed", "invalid", "failed", "skipped")


def child_skip_reason(root: str, dir: str, name: str) -> str | None:
    """子目录是否跳过：名称以 "." 或 "~$" 开头 → HIDDEN；dir/名称 不是规范路径，或 join_folder(root, dir/名称) 超过 MAX_DEPTH 层
    → INVALID_PATH；否则 None。"""
    relative = f"{dir}/{name}" if dir else name
    if name.strip().startswith((".", "~$")):
        return HIDDEN
    try:
        valid = normalize_folder_path(relative) == relative and len(join_folder(root, dir).split("/")) + 1 <= MAX_DEPTH
    except ValueError:
        valid = False
    return None if valid else INVALID_PATH


class ScanInterrupted(Exception):
    """扫描被要求停止。已写入的部分在下次扫描时跳过。"""


class MailScanService:
    """run(root, tree, *, scan_id, full, started_at, progress, should_stop) 执行一次增量扫描，返回报告：

    {"scan_id", "root", "full", "started_at", "finished_at"（clock() 的 isoformat）,
     "counts": {COUNTS 中每一项: 数量}, "items": [...], "truncated": items 是否被截断（超过 ITEM_LIMIT）}

    遍历：从根 "" 开始深度优先，子目录按名称升序（先处理完一个目录的文件，再进入它的子目录）。
    tree.mtime("") 为 None → PermanentError（根目录不存在），不保存报告。

    每个目录 d 的上次记录 prev：根目录用 catalog.dir 取；其余目录在其父目录处理完时，用一次 catalog.dirs 批量取出全部子目录的记录
    （批次 17a 修正：PG 往返次数与“有子目录的目录数”成正比，而不是与目录总数成正比）。
    每个目录 d：
    1. m = tree.mtime(d)。m 为 None（列出父目录之后消失）→ 按“子目录消失”处理（见 5），不再继续。
    2. 不是 full、prev 存在且 prev.mtime_ns == m（≥ 0）→ dirs_unchanged，不列出文件，子目录取 prev.children。
    3. 否则 tree.list(d)：抛 OSError → dirs_failed（items：{"path": d, "outcome": "dirs_failed", "error"}），
       不修改该目录的任何记录，子目录取 prev.children（没有 prev 时不再深入）。成功 → dirs_listed，然后：
       - 文件：skip_reason(FileStat(d/名称, …), extensions=MAIL_EXTENSIONS, max_bytes=MAX_MAIL_BYTES) 非 None → skipped
         （items 带 "reason"，不记录）。其余为候选，按名称升序：
         · 已有记录且 size、mtime_ns 都相同 → unchanged；
         · 否则 tree.read：异常 → failed（items 带 "error"，不记录；该目录本次的 mtime_ns 记为 -1）；
           parse_eml 抛 NotMail → invalid（记录 mail_id 为 None；原来有 mail_id 的同时从索引删除，items 带 "error"）；
           成功 → build_doc(root, 相对路径, join_folder(root, d), size, …)，原来没有记录 → new，有 → changed。
         · 原有记录中不在本次候选里的文件 → removed：从索引删除（mail_id 非空的），再删除记录。
       - 子目录：listing.dirs 中名称以 "." 或 "~$" 开头的 → dirs_skipped（reason "hidden"），
         d/名称 不是规范路径或 join_folder 后超过 MAX_DEPTH 层 → dirs_skipped（reason "invalid_path"），都不进入；
         prev.children 中不在本次子目录里的 → 按 5 删除。
       - 最后 put_dir(DirRecord(d, m 或 -1, 子目录升序, 本目录 mail_id 非空的文件数))。
    4. 写入顺序：新文档先 index.upsert，再 catalog.put_files；删除先 index.delete，再 catalog.remove_files / remove_tree。
       待写入的文档与记录积累到 BULK 条，或一个目录处理完时写入。中途失败或被打断时，未写入的文件下次重新处理。
    5. 子目录消失：对 catalog.tree_mail_ids(root, 子目录) 的每一批先 index.delete，再 catalog.remove_tree；
       dirs_removed 加该子树中的目录记录数（含自身），removed 加删除的 mail_id 数。
    - 每个目录开始前检查 should_stop()：为真时写入已处理的部分，抛 ScanInterrupted（不保存报告）。
    - 开始时 progress("0 个目录")；每处理 PROGRESS_DIRS 个目录及结束时 progress(f"{目录数} 个目录，{新增+变更} 封新邮件")。
    - items 中的 "path" 为相对根的路径（文件为 d/名称），顺序为处理顺序，超过 ITEM_LIMIT 只保留前 ITEM_LIMIT 条。
    - 完成后 catalog.save_run(报告)，并返回报告。index.upsert / delete 抛出的异常原样抛出（不保存报告）。"""

    def __init__(self, index: MailIndex, catalog: MailCatalog, *, clock: Callable[[], datetime],
                 max_bytes: int = MAX_MAIL_BYTES) -> None:
        self._index, self._catalog, self._clock, self._max_bytes = index, catalog, clock, max_bytes

    def plan(self, root: str, tree: MailTree, *, full: bool = False) -> tuple[str, ...]:
        """分片计划（批次 17b）：根目录的直接子目录名，升序。不写任何记录。
        - tree.mtime("") 为 None → PermanentError。
        - 不是 full、根目录记录存在且 mtime_ns（≥ 0）与当前相同 → 记录中的 children；
        - 否则 tree.list("")，按 child_skip_reason 去掉要跳过的；列出失败（OSError）时有记录则用记录中的 children，
          没有记录 → PermanentError。"""
        mtime = tree.mtime("")
        if mtime is None:
            raise PermanentError("邮件目录不存在")
        prev = self._catalog.dir(root, "")
        if not full and prev is not None and prev.mtime_ns >= 0 and prev.mtime_ns == mtime:
            return tuple(sorted(prev.children))
        try:
            listing = tree.list("")
        except OSError as exc:
            if prev is not None:
                return tuple(sorted(prev.children))
            raise PermanentError("邮件目录不可读") from exc
        return tuple(name for name in sorted(listing.dirs) if child_skip_reason(root, "", name) is None)

    def run(self, root: str, tree: MailTree, *, scan_id: str, full: bool = False, started_at: str | None = None,
            progress: Callable[[str], Any] = lambda text: None,
            should_stop: Callable[[], bool] = lambda: False, part: str | None = None,
            parent: str | None = None) -> dict[str, Any]:
        started = started_at or self._clock().isoformat()
        if tree.mtime("") is None:
            raise PermanentError("邮件目录不存在")
        if part is not None and part != "" and ("/" in part or child_skip_reason(root, "", part) is not None):
            raise ValueError("分片必须是根目录的直接子目录名")
        state = _Run(root, tree, self._index, self._catalog, full, self._max_bytes)
        progress("0 个目录")
        stack = [part if part is not None else ""]
        dirs = 0
        while stack:
            if should_stop():
                state.flush()
                raise ScanInterrupted("扫描已停止")
            current = stack.pop()
            children = state.visit(current)
            if part == "" and current == "":
                continue                                    # 根目录分片只处理根目录本身
            stack.extend(f"{current}/{name}" if current else name for name in sorted(children, reverse=True))
            dirs += 1
            if dirs % PROGRESS_DIRS == 0:
                progress(f"{dirs} 个目录，{state.counts['new'] + state.counts['changed']} 封新邮件")
        state.flush()
        progress(f"{dirs} 个目录，{state.counts['new'] + state.counts['changed']} 封新邮件")
        report = {"scan_id": scan_id, "root": root, "full": full, "part": part, "started_at": started,
                  "finished_at": self._clock().isoformat(), "counts": dict(state.counts),
                  "items": state.items[:ITEM_LIMIT], "truncated": len(state.items) > ITEM_LIMIT}
        if parent is not None:
            report["parent"] = parent
        self._catalog.save_run(report)
        return report


def merge_reports(parent: dict[str, Any], parts: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """整次分片扫描的报告（批次 17b）：parent 为计划时保存的报告（含 "parts": 分片数），parts 为已完成的分片报告。
    counts 为各分片之和；items 按分片的 scan_id 顺序连接，最多 ITEM_LIMIT 条；truncated：任一分片截断或连接后超过上限；
    parts_done 为已完成的分片数；全部完成时 finished_at 为各分片 finished_at 的最大值，否则为 None。
    其余字段取自 parent。不修改参数。"""
    ordered = sorted(parts, key=lambda row: row["scan_id"])
    counts = dict.fromkeys(COUNTS, 0)
    items: list[dict[str, Any]] = []
    truncated = False
    for row in ordered:
        for name in COUNTS:
            counts[name] += row["counts"].get(name, 0)
        items.extend(row["items"])
        truncated = truncated or row["truncated"]
    done = len(ordered) >= parent["parts"]
    return {**parent, "counts": counts, "items": copy.deepcopy(items[:ITEM_LIMIT]),
            "truncated": truncated or len(items) > ITEM_LIMIT, "parts_done": len(ordered),
            "finished_at": max(row["finished_at"] for row in ordered) if done and ordered else None}


class _Run:
    def __init__(self, root: str, tree: MailTree, index: MailIndex, catalog: MailCatalog, full: bool,
                 max_bytes: int) -> None:
        self.root, self.tree, self.index, self.catalog = root, tree, index, catalog
        self.full, self.max_bytes = full, max_bytes
        self.counts = dict.fromkeys(COUNTS, 0)
        self._known: dict[str, DirRecord | None] = {}    # 已批量取出、尚未处理的子目录记录
        self.items: list[dict[str, Any]] = []
        self._docs: list[MailDoc] = []
        self._records: list[tuple[str, FileRecord]] = []

    def item(self, **fields: Any) -> None:
        if len(self.items) <= ITEM_LIMIT:
            self.items.append(fields)

    def flush(self) -> None:
        if self._docs:
            self.index.upsert(self._docs)
            self._docs = []
        by_dir: dict[str, list[FileRecord]] = {}
        for dir_, record in self._records:
            by_dir.setdefault(dir_, []).append(record)
        for dir_, records in by_dir.items():
            self.catalog.put_files(self.root, dir_, records)
        self._records = []

    def _queue(self, dir_: str, record: FileRecord, doc: MailDoc | None) -> None:
        if doc is not None:
            self._docs.append(doc)
        self._records.append((dir_, record))
        if len(self._records) >= BULK:
            self.flush()

    def remove_subtree(self, dir_: str) -> None:
        removed = 0
        for batch in self.catalog.tree_mail_ids(self.root, dir_):
            self.index.delete(batch)
            removed += len(batch)
        self.counts["dirs_removed"] += self.catalog.remove_tree(self.root, dir_)
        self.counts["removed"] += removed

    def _prefetch(self, dir_: str, children: Sequence[str]) -> tuple[str, ...]:
        paths = [f"{dir_}/{name}" if dir_ else name for name in children]
        if paths:
            found = self.catalog.dirs(self.root, paths)
            self._known.update((path, found.get(path)) for path in paths)
        return tuple(children)

    def visit(self, dir_: str) -> tuple[str, ...]:
        prev = self._known.pop(dir_) if dir_ in self._known else self.catalog.dir(self.root, dir_)
        mtime = self.tree.mtime(dir_)
        if mtime is None:
            self.flush()
            self.remove_subtree(dir_)
            return ()
        if not self.full and prev is not None and prev.mtime_ns >= 0 and prev.mtime_ns == mtime:
            self.counts["dirs_unchanged"] += 1
            return self._prefetch(dir_, prev.children)
        try:
            listing = self.tree.list(dir_)
        except OSError as exc:
            self.counts["dirs_failed"] += 1
            self.item(path=dir_, outcome="dirs_failed", error=f"{type(exc).__name__}: {exc}")
            return self._prefetch(dir_, prev.children) if prev is not None else ()
        self.counts["dirs_listed"] += 1
        known = self.catalog.files(self.root, dir_)
        folder = join_folder(self.root, dir_)
        failed = False
        candidates: dict[str, FileStat] = {}
        for stat in sorted(listing.files, key=lambda row: row.path):
            relative = f"{dir_}/{stat.path}" if dir_ else stat.path
            reason = skip_reason(FileStat(relative, stat.size, stat.mtime_ns), extensions=MAIL_EXTENSIONS,
                                 max_bytes=self.max_bytes)
            if reason is not None:
                self.counts["skipped"] += 1
                self.item(path=relative, outcome="skipped", reason=reason)
                continue
            candidates[stat.path] = stat
        mails = 0
        stale: list[str] = []
        for name, stat in candidates.items():
            relative = f"{dir_}/{name}" if dir_ else name
            record = known.get(name)
            if record is not None and record.size == stat.size and record.mtime_ns == stat.mtime_ns:
                self.counts["unchanged"] += 1
                mails += record.mail_id is not None
                continue
            try:
                data = self.tree.read(dir_, name)
            except Exception as exc:
                failed = True
                self.counts["failed"] += 1
                self.item(path=relative, outcome="failed", error=f"{type(exc).__name__}: {exc}")
                if record is not None and record.mail_id is not None:
                    mails += 1
                continue
            try:
                parsed = parse_eml(data)
            except NotMail as exc:
                self.counts["invalid"] += 1
                self.item(path=relative, outcome="invalid", error=str(exc))
                if record is not None and record.mail_id is not None:
                    stale.append(record.mail_id)
                self._queue(dir_, FileRecord(name, stat.size, stat.mtime_ns, None), None)
                continue
            doc = build_doc(self.root, relative, folder, len(data), parsed)
            self.counts["new" if record is None else "changed"] += 1
            mails += 1
            self._queue(dir_, FileRecord(name, stat.size, stat.mtime_ns, doc.mail_id), doc)
        gone = [name for name in known if name not in candidates]
        self.flush()
        stale += [known[name].mail_id for name in gone if known[name].mail_id is not None]
        if stale:
            self.index.delete(stale)
        if gone:
            self.catalog.remove_files(self.root, dir_, gone)
            self.counts["removed"] += len(gone)
        children: list[str] = []
        for name in sorted(listing.dirs):
            relative = f"{dir_}/{name}" if dir_ else name
            reason = child_skip_reason(self.root, dir_, name)
            if reason is not None:
                self.counts["dirs_skipped"] += 1
                self.item(path=relative, outcome="dirs_skipped", reason=reason)
                continue
            children.append(name)
        if prev is not None:
            for name in prev.children:
                if name not in children:
                    self.remove_subtree(f"{dir_}/{name}" if dir_ else name)
        self.catalog.put_dir(self.root, DirRecord(dir_, -1 if failed else mtime, tuple(children), mails))
        return self._prefetch(dir_, children)
