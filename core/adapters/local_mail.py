"""MailTree 的本地目录实现（批次 17a）。规格见 docs/spec-30-mail.md 与 core/app/mail.py。"""
from __future__ import annotations

import os
import unicodedata
from pathlib import Path

from core.app.mail import DirListing
from core.app.ports import PermanentError
from core.domain.scan import FileStat


class LocalMailTree:
    """root 必须是已存在的绝对目录（构造时检查，否则 PermanentError）。

    - dir 参数：相对 root 的 "/" 分隔路径，根为 ""；必须不以 "/" 或 "\\\\" 开头、不含 ":" 与 ".." 段，否则 PermanentError。
    - mtime(dir)：对目标本身做一次 lstat（批次 17a 修正：不逐层检查各级父目录），返回 st_mtime_ns；
      不存在、不是目录、是符号链接或目录联接 → None。dir 参数的格式检查与 NFD 原名的对应照旧。
      不逐层检查是安全的：扫描只经由 list 发现子目录（list 已排除链接）；某个已记录的目录事后被换成链接时，
      它的父目录修改时间会变，父目录会被重新列出，链接在那里被排除。read 仍须逐层检查（原件下载不经扫描）。
    - list(dir)：os.scandir 列出直接内容：普通文件进 files（FileStat 的 path 为 NFC 规范化后的文件名，size、mtime_ns 取自 stat），
      目录进 dirs（NFC 名称）；符号链接、Windows 重解析点目录（目录联接，判定同 LocalDirectory）及其他类型都不列出。
      单个条目 stat 失败时跳过该条目。目录本身不可读 → OSError 原样抛出。
      只在 NFC 名称与磁盘原始名称不同时（例如 macOS 上的 NFD 名称）记录二者的对应，供 read 与下级目录使用；
      名称相同的不记录（数亿个文件时不能把每个名称都留在内存里）。
    - read(dir, name)：按 list 记录的原始名称读取（没有记录时用 name 本身）；目标是符号链接、解析后不在 root 之内 → PermanentError；
      读取失败 → OSError。"""

    def __init__(self, root: str | Path) -> None:
        path = Path(root)
        if not path.is_absolute() or not path.is_dir():
            raise PermanentError("邮件目录不存在或不是绝对路径")
        self._root = path.resolve()
        self._names: dict[str, str] = {}

    @property
    def root(self) -> Path:
        return self._root

    @property
    def remembered(self) -> int:
        """记下的名称对应数（测试用）。"""
        return len(self._names)

    def mtime(self, dir: str) -> int | None:
        from stat import S_ISDIR

        parts = self._parts(dir)
        target = self._root
        prefix: list[str] = []
        try:
            for part in parts:
                prefix.append(part)
                key = "/".join(prefix)
                original = self._names.get(key, part)
                if original == part and unicodedata.normalize("NFD", part) != part:
                    # 新实例未列出未变目录时，仍能恢复少量可能存在的 NFD 磁盘名。
                    with os.scandir(target) as entries:
                        alternative = None
                        for entry in entries:
                            if entry.name == part:
                                alternative = part
                                break
                            if unicodedata.normalize("NFC", entry.name) == part:
                                alternative = entry.name
                    if alternative is not None:
                        original = alternative
                        if original != part:
                            self._names[key] = original
                target /= original
            info = target.lstat()
            if not S_ISDIR(info.st_mode) or getattr(info, "st_reparse_tag", 0) in {0xA0000003, 0xA000000C}:
                return None
            return info.st_mtime_ns
        except OSError:
            return None

    def list(self, dir: str) -> DirListing:
        target = self._path(dir)
        prefix = "/".join(self._parts(dir))
        files, dirs = [], []
        names: dict[str, str] = {}
        with os.scandir(target) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                    if entry.is_symlink() or getattr(info, "st_reparse_tag", 0) in {0xA0000003, 0xA000000C}:
                        continue
                    name = unicodedata.normalize("NFC", entry.name)
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append(name)
                    elif entry.is_file(follow_symlinks=False):
                        files.append(FileStat(name, info.st_size, info.st_mtime_ns))
                    else:
                        continue
                    if name != entry.name:
                        names[(prefix + "/" if prefix else "") + name] = entry.name
                except OSError:
                    continue
        # 只保留名称实际变化的直接条目；普通 NFC 名称不随邮件总数积累。
        stem = prefix + "/" if prefix else ""
        for key in list(self._names):
            if key.startswith(stem) and "/" not in key[len(stem):]:
                del self._names[key]
        self._names.update(names)
        return DirListing(tuple(sorted(files, key=lambda item: item.path)), tuple(sorted(dirs)))

    def read(self, dir: str, name: str) -> bytes:
        if not name or "/" in name or "\\" in name or ":" in name or name in {".", ".."}:
            raise PermanentError("邮件名称不是直接文件名")
        relative = "/".join([*self._parts(dir), name])
        return self._path(relative).read_bytes()

    @staticmethod
    def _parts(relative: str) -> list[str]:
        """拒绝绝对路径、盘符和任何父目录段。"""
        parts = relative.replace("\\", "/").split("/")
        if relative.startswith(("/", "\\")) or ":" in relative or ".." in parts:
            raise PermanentError("路径不在邮件目录之内")
        return [part for part in parts if part]

    def _path(self, relative: str) -> Path:
        """逐段还原少量 NFD 原名，并检查每一层的链接及最终边界。"""
        target = self._root
        prefix = []
        for part in self._parts(relative):
            prefix.append(part)
            key = "/".join(prefix)
            parent = target
            target /= self._names.get(key, part)
            try:
                info = target.lstat()
            except FileNotFoundError:
                # 每次扫描或下载会新建实例；未列出未变目录时，按需恢复少量 NFD 原名。
                with os.scandir(parent) as entries:
                    original = next((entry.name for entry in entries
                                     if unicodedata.normalize("NFC", entry.name) == part), None)
                if original is None:
                    raise
                if original != part:
                    self._names[key] = original
                else:
                    self._names.pop(key, None)
                target = parent / original
                info = target.lstat()
            if target.is_symlink() or getattr(info, "st_reparse_tag", 0) in {0xA0000003, 0xA000000C}:
                raise PermanentError("不读取符号链接或目录联接")
        resolved = target.resolve()
        if not resolved.is_relative_to(self._root):
            raise PermanentError("路径不在邮件目录之内")
        return resolved
