"""本机 Web 数据目录：种子复制、文档记录、分块目录和未收录问题。"""
from __future__ import annotations

import copy
from collections import Counter
from collections.abc import Iterable, Mapping
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from core.app.ports import Chunk, IndexReceipt
from core.domain.purge import PURGEABLE, PURGED, tombstone
from core.app.registry import (DOCUMENT_PUBLISHED, DOCUMENT_REMOVED, EVENT_KINDS, LIFECYCLE, check_status_change, check_switch,
                               page_documents,
                               stats_of, status_event)
from core.domain.permissions import Visibility
from core.app.retrieval import ChunkCatalog
from core.domain.folders import folder_ancestors, normalize_folder_path


def now_iso() -> str:
    """使用部署所在地的中国标准时间记录界面操作。"""
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class KbStore:
    """DocumentRegistry 的 JSON 文件实现（开发与测试用）。规格见 core/app/registry.py。"""

    def __init__(self, root: str | Path, *, seed_from: str | Path | None = None) -> None:
        self.root = Path(root).resolve()
        self._lock = RLock()
        self._path = self.root / "kb.json"
        self._chunks: dict[str, list[dict[str, Any]]] = {}
        if not self._path.exists():
            if seed_from is None:
                raise ValueError("数据目录尚未初始化，请提供 --seed-from")
            seed = Path(seed_from)
            manifest = json.loads((seed / "manifest.json").read_text(encoding="utf-8"))
            documents = [{**row, "imported_at": manifest["created_at"], "origin": "seed"}
                         for row in manifest["files"]]
            state = {"dataset_id": manifest["dataset_id"], "dataset_name": manifest["dataset_name"],
                     "group_tag": "e2e", "documents": documents}
            for row in documents:
                if row["status"] == "published":
                    path = self._chunk_path(row["version_id"])
                    chunks = json.loads((seed / "chunks" / path.name).read_text(encoding="utf-8"))
                    _write_json(path, chunks)
            _write_json(self._path, state)
        self._state = json.loads(self._path.read_text(encoding="utf-8"))
        for row in self._state["documents"]:
            row.setdefault("folder", "")
        for row in self._state["documents"]:
            if row["status"] in LIFECYCLE:         # 批次 20a：已撤下、已替代的分块也保留（恢复时重新发布）
                self._chunks[row["version_id"]] = json.loads(self._chunk_path(row["version_id"]).read_text(encoding="utf-8"))
        self.startup_versions = frozenset(row["version_id"] for row in self._state["documents"])
        self._unanswered: list[dict[str, str]] = []
        unanswered_path = self.root / "unanswered.jsonl"
        if unanswered_path.exists():
            self._unanswered = [json.loads(line) for line in unanswered_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        folders_path = self.root / "folders.json"
        self._folders: set[str] = set(json.loads(folders_path.read_text(encoding="utf-8"))) if folders_path.exists() else set()
        for row in self._state["documents"]:
            self._folders.update(folder_ancestors(row["folder"]))
        events_path = self.root / "events.jsonl"
        self._events: list[dict[str, Any]] = []
        if events_path.exists():
            self._events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        offsets_path = self.root / "event_offsets.json"
        self._offsets: dict[str, int] = json.loads(offsets_path.read_text(encoding="utf-8")) if offsets_path.exists() else {}

    @property
    def dataset_id(self) -> str:
        return self._state["dataset_id"]

    @property
    def dataset_name(self) -> str:
        return self._state["dataset_name"]

    @property
    def group_tag(self) -> str:
        return self._state["group_tag"]

    def _chunk_path(self, version_id: str) -> Path:
        if re.fullmatch(r"v\d+_[0-9a-fA-F]{10}", version_id) is None:
            raise ValueError("文档版本号格式无效")
        return self.root / "chunks" / f"{version_id}.json"

    def documents(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._state["documents"])

    def detail(self, version_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = next((row for row in self._state["documents"] if row["version_id"] == version_id), None)
            return None if row is None else copy.deepcopy(
                {"document": row, "chunks": self._chunks.get(version_id, []) if row["status"] == "published" else []})

    def save_document(self, record: dict[str, Any], chunks: list[dict[str, Any]]) -> None:
        """先保存分块，再原子提交索引；只追加本次作业的新版本。"""
        with self._lock:
            version = record["version_id"]
            if any(row["version_id"] == version for row in self._state["documents"]):
                raise ValueError("不得覆盖已有文档记录")
            record = {**copy.deepcopy(record), "folder": normalize_folder_path(record.get("folder", ""))}
            self._chunk_path(version)
            self.ensure_folder(record["folder"])
            updated = copy.deepcopy(self._state)
            updated["documents"].append(record)
            if record["status"] == "published":
                _write_json(self._chunk_path(version), chunks)
            _write_json(self._path, updated)
            self._state = updated
            if record["status"] == "published":
                self._chunks[version] = copy.deepcopy(chunks)
                self.append_event(DOCUMENT_PUBLISHED, version, {"folder": record["folder"], "title": record["title"]})

    def next_version_number(self) -> int:
        with self._lock:
            existing = max((int(match.group(1)) for row in self._state["documents"]
                            if (match := re.match(r"^v(\d+)_", row["version_id"]))), default=0)
            number = max(self._state.get("next_version", 1), existing + 1)
            updated = {**copy.deepcopy(self._state), "next_version": number + 1}
            _write_json(self._path, updated)
            self._state = updated
            return number

    def set_review(self, version_id: str, review: dict[str, Any] | None) -> dict[str, Any]:
        with self._lock:
            updated = copy.deepcopy(self._state)
            row = next((row for row in updated["documents"] if row["version_id"] == version_id), None)
            if row is None:
                raise KeyError(version_id)
            if review is None:
                row.pop("review", None)
            else:
                row["review"] = copy.deepcopy(review)
            _write_json(self._path, updated)
            self._state = updated
            return copy.deepcopy(row)

    def set_status(self, version_id: str, status: str, entry: dict[str, Any], *,
                   chunk_map: Mapping[str, str] | None = None, engine_doc_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            updated = copy.deepcopy(self._state)
            row = next((row for row in updated["documents"] if row["version_id"] == version_id), None)
            if row is None:
                raise KeyError(version_id)
            chunks = copy.deepcopy(self._chunks.get(version_id, []))
            check_status_change(row["status"], status, (c["chunk_id"] for c in chunks), chunk_map, engine_doc_id)
            row["status"] = status
            row["lifecycle"] = [*row.get("lifecycle", []), copy.deepcopy(entry)]
            if status == "published":
                row["engine_doc_id"] = engine_doc_id
                for chunk in chunks:
                    chunk["engine_chunk_id"] = chunk_map[chunk["chunk_id"]]
                _write_json(self._chunk_path(version_id), chunks)
            _write_json(self._path, updated)
            self._state = updated
            self._chunks[version_id] = chunks
            kind, payload = status_event(row, status)
            self.append_event(kind, version_id, payload)
            return copy.deepcopy(row)

    def switch_index(self, dataset_id: str, dataset_name: str,
                     mapping: Mapping[str, tuple[str, Mapping[str, str]]]) -> None:
        with self._lock:
            published = {row["version_id"]: [c["chunk_id"] for c in self._chunks.get(row["version_id"], [])]
                         for row in self._state["documents"] if row["status"] == "published"}
            check_switch(dataset_id, dataset_name, published, mapping)
            updated = copy.deepcopy(self._state)
            chunks = {version: copy.deepcopy(self._chunks.get(version, [])) for version in published}
            for row in updated["documents"]:
                if row["version_id"] in published:
                    engine_doc_id, chunk_map = mapping[row["version_id"]]
                    row["engine_doc_id"] = engine_doc_id
                    for chunk in chunks[row["version_id"]]:
                        chunk["engine_chunk_id"] = chunk_map[chunk["chunk_id"]]
            updated["dataset_id"], updated["dataset_name"] = dataset_id, dataset_name
            for version, rows in chunks.items():                   # 先写分块，最后原子替换索引文件
                _write_json(self._chunk_path(version), rows)
            _write_json(self._path, updated)
            self._state = updated
            self._chunks.update(chunks)

    def purge(self, version_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            updated = copy.deepcopy(self._state)
            index = next((i for i, row in enumerate(updated["documents"]) if row["version_id"] == version_id), None)
            if index is None:
                raise KeyError(version_id)
            row = updated["documents"][index]
            if row["status"] not in PURGEABLE:
                raise ValueError("只能彻底删除已撤下或已替代的文档")
            stone = tombstone(row, entry)
            updated["documents"][index] = stone
            _write_json(self._path, updated)
            self._state = updated
            self._chunks.pop(version_id, None)
            self._chunk_path(version_id).unlink(missing_ok=True)
            self.append_event(DOCUMENT_REMOVED, version_id, {"folder": stone["folder"], "title": stone["title"],
                                                             "status": PURGED})
            return copy.deepcopy(stone)

    def stored_chunks(self, version_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._chunks.get(version_id, []))

    def document_page(self, scope: Visibility | None = None, *, status: str | None = None, query: str | None = None,
                      offset: int = 0, limit: int = 50) -> dict[str, Any]:
        with self._lock:
            return page_documents(self._state["documents"], scope, status=status, query=query, offset=offset,
                                  limit=limit)

    def document_stats(self, scope: Visibility | None = None) -> dict[str, Any]:
        with self._lock:
            return stats_of(self._state["documents"], scope)

    def folder_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(Counter(row.get("folder", "") for row in self._state["documents"]))

    def folders_of(self, version_ids: Iterable[str]) -> dict[str, str]:
        wanted = set(version_ids)
        with self._lock:
            return {row["version_id"]: row.get("folder", "") for row in self._state["documents"]
                    if row["version_id"] in wanted}

    def records(self, version_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        wanted = set(version_ids)
        with self._lock:
            return {row["version_id"]: copy.deepcopy(row) for row in self._state["documents"] if row["version_id"] in wanted}

    def find_by_digest(self, sha256: str) -> str | None:
        prefix = _digest_prefix(sha256)
        with self._lock:
            return next((row["version_id"] for row in self._state["documents"]
                         if row["version_id"].partition("_")[2].lower() == prefix and row["status"] != PURGED), None)

    def find_by_hash(self, content_hash: str) -> str | None:
        with self._lock:
            return next((row["version_id"] for row in self._state["documents"]
                         if row["status"] == "published" and row.get("content_hash") == content_hash), None)

    def context(self) -> tuple[ChunkCatalog, dict[str, str], dict[str, str]]:
        """为一次请求创建一致的目录快照，隔离后台导入的并发更新。"""
        with self._lock:
            catalog = ChunkCatalog()
            titles, filenames = {}, {}
            for row in self._state["documents"]:
                version = row["version_id"]
                titles[version], filenames[version] = row["title"], row["filename"]
                if row["status"] != "published":
                    continue
                rows = self._chunks.get(version, [])
                chunks = [Chunk(r["chunk_id"], r["text"], r["page_start"], r["page_end"], r["section_path"],
                                copy.deepcopy(r["meta"])) for r in rows]
                catalog.add(IndexReceipt(version, row["engine_doc_id"], len(chunks),
                                         {r["chunk_id"]: r["engine_chunk_id"] for r in rows}), chunks)
            return catalog, titles, filenames

    def record_unanswered(self, question: str, status: str) -> None:
        with self._lock:
            row = {"time": now_iso(), "question": question, "status": status}
            with (self.root / "unanswered.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._unanswered.append(row)

    def unanswered(self) -> list[dict[str, str]]:
        with self._lock:
            return copy.deepcopy(self._unanswered)

    def ensure_folder(self, path: str) -> str:
        path = normalize_folder_path(path)
        with self._lock:
            updated = self._folders | set(folder_ancestors(path))
            if updated != self._folders:
                _write_json(self.root / "folders.json", sorted(updated))
                self._folders = updated
        return path

    def folders(self) -> list[str]:
        with self._lock:
            return sorted(self._folders)

    def append_event(self, kind: str, version_id: str | None = None, payload: dict[str, Any] | None = None) -> int:
        if kind not in EVENT_KINDS:
            raise ValueError("未知知识事件类型")
        with self._lock:
            seq = self._events[-1]["seq"] + 1 if self._events else 1
            event = {"seq": seq, "kind": kind, "version_id": version_id,
                     "payload": copy.deepcopy(payload) if payload is not None else {}, "time": now_iso()}
            serialized = json.dumps(event, ensure_ascii=False)
            with (self.root / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(serialized + "\n")
            self._events.append(event)
            return seq

    def events(self, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("事件读取条数必须大于零")
        with self._lock:
            return copy.deepcopy([event for event in self._events if event["seq"] > after][:limit])

    def last_event_seq(self) -> int:
        with self._lock:
            return len(self._events)

    def offset(self, subscriber: str) -> int:
        with self._lock:
            return self._offsets.get(subscriber, 0)

    def commit_offset(self, subscriber: str, seq: int) -> None:
        with self._lock:
            current = self._offsets.get(subscriber, 0)
            maximum = self._events[-1]["seq"] if self._events else 0
            if seq < current or seq > maximum:
                raise ValueError("订阅进度不能后退或超过最大事件序号")
            if seq != current:
                updated = {**self._offsets, subscriber: seq}
                _write_json(self.root / "event_offsets.json", updated)
                self._offsets = updated


def _digest_prefix(sha256: str) -> str:
    """校验 sha256 并返回小写前 10 位（find_by_digest 共用）。"""
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None:
        raise ValueError("sha256 必须是 64 位十六进制")
    return sha256[:10].lower()
