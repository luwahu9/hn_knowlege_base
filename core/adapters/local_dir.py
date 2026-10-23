"""SourceTree 的本地目录实现（批次 15d）。规格见 docs/spec-27-folder-import.md 与 core/app/scan.py。"""
from __future__ import annotations

import os
import unicodedata
from collections.abc import Iterator
from pathlib import Path

from core.app.ports import PermanentError
from core.domain.scan import FileStat


_LINK_TAGS = {0xA0000003, 0xA000000C}     # IO_REPARSE_TAG_MOUNT_POINT（目录联接）、IO_REPARSE_TAG_SYMLINK


def _is_reparse_point(entry: os.DirEntry[str]) -> bool:
    """目录是否为 Windows 的目录联接（junction）或目录符号链接：这类目录不进入（Python 3.11 没有 DirEntry.is_junction）。
    按重解析标记判断，OneDrive 等云同步目录虽带重解析属性但标记不同，照常进入。"""
    return getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0) in _LINK_TAGS


class LocalDirectory:
    """root 必须是已存在的绝对目录路径（构造时检查，否则 PermanentError）。

    files()：os.scandir 递归遍历（不跟随符号链接；符号链接与非普通文件不列出，不进入 Windows 重解析点目录（含目录联接）；
    不可读的子目录跳过），
    path 为相对 root 的 "/" 分隔路径并做 NFC 规范化，size、mtime_ns 取自 stat。遍历顺序不作保证。
    read(path)：path 必须是相对路径、不含 ".." 段；解析后（resolve）必须仍在 root 之内且不是符号链接，否则 PermanentError。
    NFC 规范化后的 path 在磁盘上可能是 NFD 名字（macOS 等），读取时按 files() 记录的原始名字找。"""

    def __init__(self, root: str | Path) -> None:
        path = Path(root)
        if not path.is_absolute() or not path.is_dir():
            raise PermanentError("扫描目录不存在或不是绝对路径")
        self._root = path.resolve()
        self._names: dict[str, str] = {}

    @property
    def root(self) -> Path:
        return self._root

    def files(self) -> Iterator[FileStat]:
        if not self._root.is_dir():
            raise PermanentError("扫描目录不存在")
        names: dict[str, str] = {}
        stack = [""]
        while stack:
            relative = stack.pop()
            try:
                entries = list(os.scandir(self._root / relative if relative else self._root))
            except OSError:
                if not relative:
                    raise PermanentError("扫描目录不可读") from None
                continue
            for entry in entries:
                raw = f"{relative}/{entry.name}" if relative else entry.name
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if not _is_reparse_point(entry):
                            stack.append(raw)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                path = unicodedata.normalize("NFC", raw)
                names[path] = raw
                yield FileStat(path, stat.st_size, stat.st_mtime_ns)
        self._names = names

    def read(self, path: str) -> bytes:
        if not path or path.startswith(("/", "\\")) or ":" in path or ".." in path.replace("\\", "/").split("/"):
            raise PermanentError("路径不在扫描目录之内")
        raw = self._names.get(path, path)
        target = self._root / raw
        if target.is_symlink():
            raise PermanentError("不读取符号链接")
        resolved = target.resolve()
        if resolved != self._root and self._root not in resolved.parents:
            raise PermanentError("路径不在扫描目录之内")
        return resolved.read_bytes()
