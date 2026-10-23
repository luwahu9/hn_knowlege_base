"""MailIndex 与 MailCatalog 的内存实现（批次 17a）：测试与开放模式演示使用，也是契约的参照实现。"""
from __future__ import annotations

import copy
from collections.abc import Collection, Iterator, Sequence
from threading import RLock
from typing import Any

from core.app.mail import (BULK, hit_from_doc, RECIPIENTS_SHOWN, RELEVANT_LIMIT_MAX, THREAD_LIMIT_MAX, DirRecord, FileRecord, MailHit,
                           MailPage)
from core.domain.mail_qa import ThreadKey, in_thread, question_keys, relevance, thread_order
from core.domain.folders import in_folder
from core.domain.mail import MailDoc, make_snippet
from core.domain.mail_query import TOTAL_CAP, MailQuery, check_page, mail_matches, result_order
from core.domain.permissions import FolderScope
from core.domain.scan import join_folder


hit_of = hit_from_doc


class InMemoryMailIndex:
    def __init__(self) -> None:
        self._docs: dict[str, MailDoc] = {}
        self._lock = RLock()

    def ensure(self) -> None:
        return None

    def upsert(self, docs: Sequence[MailDoc]) -> None:
        with self._lock:
            for doc in docs:
                self._docs[doc.mail_id] = doc

    def delete(self, mail_ids: Collection[str]) -> None:
        with self._lock:
            for mail_id in mail_ids:
                self._docs.pop(mail_id, None)

    def get(self, mail_id: str) -> MailDoc | None:
        with self._lock:
            return self._docs.get(mail_id)

    def search(self, query: MailQuery, *, scope: FolderScope | None, offset: int, limit: int) -> MailPage:
        check_page(offset, limit)
        with self._lock:
            matched = sorted((doc for doc in self._docs.values()
                              if (scope is None or scope.contains(doc.folder)) and mail_matches(doc, query)),
                             key=result_order)
        rows = tuple(hit_of(doc, query.terms) for doc in matched[offset:offset + limit])
        return MailPage(rows, min(len(matched), TOTAL_CAP), len(matched) <= TOTAL_CAP)

    def relevant(self, text: str, query: MailQuery, *, scope: FolderScope | None, limit: int) -> list[MailDoc]:
        if not 1 <= limit <= RELEVANT_LIMIT_MAX:
            raise ValueError("候选数超出范围")
        keys = question_keys(text)
        if not keys:
            return []
        with self._lock:
            scored = [(relevance(doc, keys), doc) for doc in self._docs.values()
                      if (scope is None or scope.contains(doc.folder)) and mail_matches(doc, query)]
        scored = [item for item in scored if item[0] > 0]
        scored.sort(key=lambda item: (-item[0], result_order(item[1])))
        return [doc for _, doc in scored[:limit]]

    def thread(self, key: ThreadKey, *, scope: FolderScope | None, limit: int) -> list[MailHit]:
        if not 1 <= limit <= THREAD_LIMIT_MAX:
            raise ValueError("会话记录数超出范围")
        with self._lock:
            members = [doc for doc in self._docs.values()
                       if (scope is None or scope.contains(doc.folder)) and in_thread(doc, key)]
        members.sort(key=thread_order)
        return [hit_of(doc, ()) for doc in members[:limit]]

    def count(self) -> int:
        with self._lock:
            return len(self._docs)

    def refresh(self) -> None:
        return None


class InMemoryMailCatalog:
    def __init__(self) -> None:
        self._dirs: dict[tuple[str, str], DirRecord] = {}
        self._files: dict[tuple[str, str], dict[str, FileRecord]] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    def dir(self, root: str, dir: str) -> DirRecord | None:
        with self._lock:
            return self._dirs.get((root, dir))

    def dirs(self, root: str, dirs: Collection[str]) -> dict[str, DirRecord]:
        with self._lock:
            return {dir_: self._dirs[(root, dir_)] for dir_ in dirs if (root, dir_) in self._dirs}

    def put_dir(self, root: str, record: DirRecord) -> None:
        with self._lock:
            self._dirs[(root, record.dir)] = record

    def files(self, root: str, dir: str) -> dict[str, FileRecord]:
        with self._lock:
            return dict(self._files.get((root, dir), {}))

    def put_files(self, root: str, dir: str, records: Sequence[FileRecord]) -> None:
        with self._lock:
            target = self._files.setdefault((root, dir), {})
            for record in records:
                target[record.name] = record

    def remove_files(self, root: str, dir: str, names: Collection[str]) -> None:
        with self._lock:
            target = self._files.get((root, dir), {})
            for name in names:
                target.pop(name, None)

    def _under(self, root: str, dir: str, keys: Collection[tuple[str, str]]) -> list[tuple[str, str]]:
        return sorted(key for key in keys if key[0] == root and in_folder(key[1], dir))

    def tree_mail_ids(self, root: str, dir: str, *, batch: int = BULK) -> Iterator[list[str]]:
        with self._lock:
            ids = [record.mail_id for key in self._under(root, dir, self._files)
                   for record in sorted(self._files[key].values(), key=lambda row: row.name)
                   if record.mail_id is not None]
        for start in range(0, len(ids), batch):
            yield ids[start:start + batch]

    def remove_tree(self, root: str, dir: str) -> int:
        with self._lock:
            dirs = self._under(root, dir, self._dirs)
            for key in dirs:
                del self._dirs[key]
            for key in self._under(root, dir, self._files):
                del self._files[key]
            return len(dirs)

    def folder_counts(self, *, depth: int) -> dict[str, int]:
        if depth < 1:
            raise ValueError("depth 必须为正数")
        counts: dict[str, int] = {}
        with self._lock:
            for (root, dir_), record in self._dirs.items():
                if record.mails > 0:
                    key = "/".join(join_folder(root, dir_).split("/")[:depth])
                    counts[key] = counts.get(key, 0) + record.mails
        return dict(sorted(counts.items()))

    def stats(self, root: str) -> dict[str, int]:
        with self._lock:
            rows = [record for key, record in self._dirs.items() if key[0] == root]
            return {"dirs": len(rows), "mails": sum(record.mails for record in rows)}

    def save_run(self, report: dict[str, Any]) -> None:
        with self._lock:
            self._runs[report["scan_id"]] = copy.deepcopy(report)

    def runs(self, root: str, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit 必须为正数")
        with self._lock:
            rows = sorted((row for row in self._runs.values() if row["root"] == root and "parent" not in row),
                          key=lambda row: (row["started_at"], row["scan_id"]), reverse=True)
            return copy.deepcopy(rows[:limit])

    def parts(self, scan_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(sorted((row for row in self._runs.values() if row.get("parent") == scan_id),
                                        key=lambda row: row["scan_id"]))

    def run(self, scan_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._runs.get(scan_id)
            return copy.deepcopy(row) if row is not None else None
