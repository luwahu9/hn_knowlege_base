"""可续传上传的暂存区（批次 15d）。规格见 docs/spec-27-folder-import.md 第二节。

暂存目录下每个上传两个文件：<sha256>.part（已收到的字节）与 <sha256>.json（{"size","path","updated_at"}）。
upload_id 即原件 sha256（小写）：同一内容只有一个上传，换一个文件夹选同样的文件也能续传。
只在 Web 进程中使用；同一进程内用锁串行化。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any

from apps.web.store import _write_json

_ID = re.compile(r"[0-9a-f]{64}")
CHUNK_MAX = 8 * 1024 * 1024           # 单次 PUT 的最大字节数
TTL_HOURS = 24                        # 超过这么久没有写入的上传在 purge 时删除


class OffsetMismatch(Exception):
    """PUT 的 offset 与已收到的字节数不一致。received：服务端已收到的字节数。"""

    def __init__(self, received: int) -> None:
        super().__init__("上传位置不一致")
        self.received = received


class UploadStaging:
    """begin / status / append / finish / discard / purge，见各方法。所有 upload_id、sha256 参数先转小写再校验，
    不是 64 位十六进制 → ValueError。"""

    def __init__(self, root: str | Path, *, max_bytes: int, clock: Callable[[], datetime],
                 chunk_max: int = CHUNK_MAX, ttl_hours: int = TTL_HOURS) -> None:
        self._root = Path(root)
        self._max, self._clock, self._chunk_max, self._ttl = max_bytes, clock, chunk_max, timedelta(hours=ttl_hours)
        self._lock = RLock()

    @staticmethod
    def _id(value: str) -> str:
        value = value.lower() if isinstance(value, str) else ""
        if _ID.fullmatch(value) is None:
            raise ValueError("上传编号必须是 64 位十六进制")
        return value

    def _paths(self, upload_id: str) -> tuple[Path, Path]:
        return self._root / f"{upload_id}.part", self._root / f"{upload_id}.json"

    def _meta(self, upload_id: str) -> dict[str, Any] | None:
        part, meta = self._paths(upload_id)
        if not meta.exists() or not part.exists():
            return None
        return json.loads(meta.read_text(encoding="utf-8"))

    def begin(self, sha256: str, size: int, path: str) -> dict[str, Any]:
        """开始或续传：返回 {"upload_id", "received", "size"}。
        size 必须在 1..max_bytes 之间，否则 ValueError。已有同 sha256 的上传且 size 相同 → 保留已收到的字节，
        更新 path；size 不同 → 清空重来。"""
        upload_id = self._id(sha256)
        if not isinstance(size, int) or not 1 <= size <= self._max:
            raise ValueError("文件大小无效或超过上限")
        with self._lock:
            part, meta = self._paths(upload_id)
            current = self._meta(upload_id)
            if current is None or current["size"] != size:
                self._root.mkdir(parents=True, exist_ok=True)
                part.write_bytes(b"")
            _write_json(meta, {"size": size, "path": path, "updated_at": self._clock().isoformat()})
            return {"upload_id": upload_id, "received": part.stat().st_size, "size": size}

    def status(self, upload_id: str) -> dict[str, Any] | None:
        """{"upload_id", "received", "size", "path"}；没有为 None。"""
        upload_id = self._id(upload_id)
        with self._lock:
            current = self._meta(upload_id)
            if current is None:
                return None
            return {"upload_id": upload_id, "received": self._paths(upload_id)[0].stat().st_size,
                    "size": current["size"], "path": current["path"]}

    def append(self, upload_id: str, offset: int, data: bytes) -> int:
        """在 offset 处追加 data，返回已收到的字节数。
        上传不存在 → KeyError；offset 不等于已收到的字节数 → OffsetMismatch；data 为空或超过 chunk_max、
        追加后超过 size → ValueError（不写入）。"""
        upload_id = self._id(upload_id)
        with self._lock:
            current = self._meta(upload_id)
            if current is None:
                raise KeyError(upload_id)
            part, meta = self._paths(upload_id)
            received = part.stat().st_size
            if offset != received:
                raise OffsetMismatch(received)
            if not data or len(data) > self._chunk_max or received + len(data) > current["size"]:
                raise ValueError("分块大小无效")
            with part.open("ab") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _write_json(meta, {**current, "updated_at": self._clock().isoformat()})
            return received + len(data)

    def finish(self, upload_id: str) -> bytes:
        """完成：返回全部内容并删除暂存文件。上传不存在 → KeyError；未收齐 → ValueError（保留）；
        内容的 sha256 与 upload_id 不符 → ValueError（删除暂存文件，需要重新上传）。"""
        upload_id = self._id(upload_id)
        with self._lock:
            current = self._meta(upload_id)
            if current is None:
                raise KeyError(upload_id)
            part, _ = self._paths(upload_id)
            if part.stat().st_size != current["size"]:
                raise ValueError("文件尚未上传完整")
            data = part.read_bytes()
            self.discard(upload_id)
            if hashlib.sha256(data).hexdigest() != upload_id:
                raise ValueError("文件内容与哈希不符，请重新上传")
            return data

    def discard(self, upload_id: str) -> None:
        """删除暂存文件；不存在则忽略。"""
        upload_id = self._id(upload_id)
        with self._lock:
            for path in self._paths(upload_id):
                path.unlink(missing_ok=True)

    def purge(self) -> int:
        """删除超过 ttl 没有写入的上传与残缺的暂存文件，返回删除的上传数。"""
        removed = 0
        with self._lock:
            if not self._root.exists():
                return 0
            limit = self._clock() - self._ttl
            ids = {path.stem for path in self._root.iterdir() if path.suffix in (".part", ".json")
                   and _ID.fullmatch(path.stem)}
            for upload_id in sorted(ids):
                current = self._meta(upload_id)
                if current is None or datetime.fromisoformat(current["updated_at"]) < limit:
                    self.discard(upload_id)
                    removed += 1
        return removed
