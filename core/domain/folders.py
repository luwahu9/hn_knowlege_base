"""文件夹路径：导入时的相对路径即权限单位。纯逻辑，不触碰文件系统。

规范形式：以 "/" 分隔、首尾无 "/"、无空段、无 "." 与 ".."，Unicode NFC，保留大小写；根文件夹为空串 ""。
"""
from __future__ import annotations

import unicodedata

MAX_DEPTH = 32
MAX_SEGMENT = 255
_FORBIDDEN = set('<>:"|?*') | {chr(code) for code in range(32)} | {"\x7f"}


def normalize_folder_path(raw: str) -> str:
    """把用户或浏览器给出的相对路径化为规范形式。

    - 先检查原始字符串：含任何禁用字符（含制表符、换行等控制字符，无论在首尾还是中间）→ ValueError。
    - 再处理："\\" 视同 "/"；连续分隔符、首尾分隔符、"." 段被忽略；每段去掉首尾空白（此时已不含控制字符）。
    - ".." 段、绝对盘符（如 "C:"）、超过 MAX_DEPTH 层或单段超过 MAX_SEGMENT 字符 → ValueError。
    - 只含空白或分隔符 → ""（根）。
    """
    if not isinstance(raw, str) or any(char in _FORBIDDEN for char in raw):
        raise ValueError("文件夹路径含禁用字符或类型无效")
    parts: list[str] = []
    for part in unicodedata.normalize("NFC", raw.replace("\\", "/")).split("/"):
        part = part.strip()
        if not part or part == ".":
            continue
        if part == ".." or len(part) > MAX_SEGMENT:
            raise ValueError("文件夹路径段无效")
        parts.append(part)
    if len(parts) > MAX_DEPTH:
        raise ValueError("文件夹层级过深")
    return "/".join(parts)


def folder_ancestors(path: str) -> list[str]:
    """规范路径的全部祖先（不含根 ""，含自身），由浅到深。"a/b/c" → ["a", "a/b", "a/b/c"]；"" → []。

    参数必须已是规范形式（normalize_folder_path(path) == path），否则 ValueError。
    """
    if normalize_folder_path(path) != path:
        raise ValueError("文件夹路径必须已规范化")
    parts = path.split("/") if path else []
    return ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]


def folder_parent(path: str) -> str | None:
    """规范路径的父路径；根 "" 没有父路径，返回 None；"a" 的父路径是 ""。"""
    folder_ancestors(path)
    return path.rpartition("/")[0] if path else None


def folder_name(path: str) -> str:
    """末段名称；根为 ""。"""
    folder_ancestors(path)
    return path.rsplit("/", 1)[-1]


def in_folder(document_folder: str, folder: str) -> bool:
    """document_folder 是否位于 folder 之内（含自身与全部子孙）。根 "" 包含一切。按段比较，"ab" 不在 "a" 之内。"""
    folder_ancestors(document_folder)
    folder_ancestors(folder)
    return not folder or document_folder == folder or document_folder.startswith(folder + "/")
