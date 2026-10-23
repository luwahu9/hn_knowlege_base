"""BlobStore 的本地文件系统实现。规格：docs/spec-12-e2e-retrieval.md

用于单机部署和端到端测试：对象按键存放在 root 目录下，键的 "/" 对应子目录。
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from core.app.ports import PermanentError

_SEGMENT_FORBIDDEN = re.compile(r'[\\:*?"<>|\x00-\x1f]')


def validate_key(key: str) -> list[str]:
    """校验对象键并返回各段。不合法 → PermanentError。

    合法的键：非空；用 "/" 分段；每段非空、不是 "." 或 ".."、不以空格或 "." 结尾、
    不含反斜杠、冒号、* ? " < > |、控制字符；总长不超过 1024。
    """
    parts = key.split("/")
    if (not key or len(key) > 1024 or any(
        not part or part in {".", ".."} or part.endswith((" ", "."))
        or _SEGMENT_FORBIDDEN.search(part) for part in parts
    )):
        raise PermanentError("不合法的对象键")
    return parts


class FileSystemBlobStore:
    """root 不存在时自动创建。signed_url 返回 "<文件 URI>?ttl=<秒数>"（本地文件，无真正签名）。

    写入：先写同目录下的临时文件，再原子改名为目标文件；目标已存在时比较内容，
    相同视为幂等成功，不同 → PermanentError 且保留原内容。
    键对应的路径被目录占用（例如已有 "a/1" 时写 "a"），或其父路径被文件占用 → PermanentError。
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        from threading import RLock

        self._lock = RLock()
        try:
            self._root = Path(root).resolve()
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PermanentError(f"无法创建对象目录：{exc}") from exc

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        import errno

        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            target = self._path(key)
            temporary: Path | None = None
            try:
                if self._same_existing(target, data):
                    return digest
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(data)
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    self._same_existing(target, data)
                except OSError as exc:
                    if exc.errno not in {errno.ENOSYS, errno.ENOTSUP, errno.EPERM, errno.EXDEV}:
                        raise
                    if not self._same_existing(target, data):
                        os.replace(temporary, target)
                return digest
            except OSError as exc:
                raise PermanentError(f"对象写入失败：{exc}") from exc
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def get(self, key: str) -> bytes:
        with self._lock:
            try:
                return self._path(key).read_bytes()
            except OSError as exc:
                raise PermanentError(f"对象读取失败：{exc}") from exc

    def exists(self, key: str) -> bool:
        with self._lock:
            return self._path(key).is_file()

    def signed_url(self, key: str, *, ttl_seconds: int) -> str:
        if not 0 < ttl_seconds <= 3600:
            raise PermanentError("有效期须在 1 到 3600 秒之间")
        with self._lock:
            target = self._path(key)
            if not target.is_file():
                raise PermanentError("对象不存在")
            return f"{target.as_uri()}?ttl={ttl_seconds}"

    def delete_prefix(self, prefix: str) -> int:
        """前缀对应的目录整个删除（键按 "/" 分段存为目录，前缀以 "/" 结尾即对应一个目录）。"""
        import shutil

        if not prefix.endswith("/"):
            raise PermanentError("前缀必须以 / 结尾")
        with self._lock:
            folder = self._path(prefix[:-1])
            if not folder.is_dir():
                return 0
            count = sum(1 for item in folder.rglob("*") if item.is_file())
            try:
                shutil.rmtree(folder)
            except OSError as exc:
                raise PermanentError(f"对象删除失败：{exc}") from exc
            return count

    def _path(self, key: str) -> Path:
        try:
            target = self._root.joinpath(*validate_key(key)).resolve()
            if not target.is_relative_to(self._root):
                raise PermanentError("对象路径超出根目录")
            return target
        except OSError as exc:
            raise PermanentError(f"对象路径不可用：{exc}") from exc

    def _same_existing(self, target: Path, data: bytes) -> bool:
        if not target.exists():
            return False
        if not target.is_file():
            raise PermanentError("对象键被目录占用")
        if target.read_bytes() != data:
            raise PermanentError("对象键已存在且内容不同")
        return True
