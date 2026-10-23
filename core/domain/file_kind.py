"""按文件头识别真实格式（纯逻辑）。规格：docs/spec-23-real-docs.md

真实资料里常有改了扩展名的文件（老格式 .doc 被命名为 .docx 等）。转换器按扩展名选择解析方式，
所以入库前先按文件头确定“有效扩展名”。
"""
from __future__ import annotations

_ZIP_FAMILY = frozenset({"docx", "xlsx", "pptx", "odt", "ods", "odp", "epub", "zip"})
_OLE_FAMILY = {"doc": "doc", "docx": "doc", "dot": "doc", "dotx": "doc",
               "xls": "xls", "xlsx": "xls", "ppt": "ppt", "pptx": "ppt"}
_TEXT_FAMILY = frozenset({"md", "csv", "html", "htm", "txt", "xml", "json"})
_IMAGES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"), (b"II*\x00", "tif"), (b"MM\x00*", "tif"), (b"BM", "bmp"),
)


def effective_extension(head: bytes, filename: str) -> str | None:
    """返回用于转换的扩展名（小写、不含点）；无法确认时返回 None。head 为文件开头若干字节（调用方传前 16 字节）。
    各文件头只按其自身所需的字节数判断（不设统一门槛）：OLE 需要完整 8 字节，WEBP 需要覆盖 head[8:12]，ZIP 只需 4 字节，BMP 只需 2 字节。

    ext = 文件名的小写扩展名（没有则为 ""）。
    - head 以 b"%PDF-" 开头 → "pdf"。
    - head 以 OLE 复合文档头 D0 CF 11 E0 A1 B1 1A E1 开头 → ext 在 _OLE_FAMILY 中则取其映射（如 docx → doc），否则 None。
    - head 以 b"PK\\x03\\x04" 开头 → ext 在 _ZIP_FAMILY 中则为 ext，否则 None。
    - head 以 b"RIFF" 开头且 head[8:12] == b"WEBP" → "webp"；以 _IMAGES 中某个文件头开头 → 对应扩展名
      （ext 为 "jpeg" 且识别为 "jpg" 时返回 "jpeg"，"tiff" 与 "tif" 同理，保持原扩展名写法）。
    - head 以 b"{\\rtf" 开头 → "rtf"。
    - 以上都不是：ext 在 _TEXT_FAMILY 中 → ext（文本格式无可靠文件头，信任扩展名）；否则 None。
    """
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    ext = basename.rsplit(".", 1)[1].lower() if "." in basename else ""
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return _OLE_FAMILY.get(ext)
    if head.startswith(b"PK\x03\x04"):
        return ext if ext in _ZIP_FAMILY else None
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    for signature, kind in _IMAGES:
        if head.startswith(signature):
            return ext if (kind, ext) in {("jpg", "jpeg"), ("tif", "tiff")} else kind
    if head.startswith(b"{\\rtf"):
        return "rtf"
    return ext if ext in _TEXT_FAMILY else None


def with_extension(filename: str, extension: str) -> str:
    """把文件名的扩展名替换为 extension（没有扩展名则追加）。"""
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    return f"{stem}.{extension}"


_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ENCRYPTED_STREAM = "EncryptedPackage".encode("utf-16-le")


def is_encrypted_office(data: bytes) -> bool:
    """是否为加密（设了打开密码）的 Office 文档：以 OLE 复合文档头开头，且内容中出现 UTF-16LE 编码的流名
    “EncryptedPackage”（加密后的 docx/xlsx/pptx 都以这种 OLE 容器保存，文件头与老格式 .doc 相同）。"""
    return data.startswith(_OLE_MAGIC) and _ENCRYPTED_STREAM in data

