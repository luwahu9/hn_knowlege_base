"""Docling Serve 适配器（Converter 端口）。规格：docs/spec-10-adapters.md 第一部分。"""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

import httpx

from core.app.ports import (
    BlobStore, ConvertResult, PermanentError, TransientError, UnsupportedFormatError,
)

# 第一条链路支持的扩展名（小写，不含点）。PowerPoint（ppt、pptx）暂不支持。
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    "pdf", "docx", "xlsx", "html", "htm", "csv", "md", "odt", "epub",
    "doc", "xls", "rtf",
    "png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp",
})
PARSER_NAME = "docling"


def extension_of(filename: str) -> str:
    """小写扩展名，不含点；兼容 Windows 与 POSIX 路径；没有扩展名返回空串。"""
    name = PureWindowsPath(PurePosixPath(filename).name).name
    return name.rpartition(".")[2].lower() if "." in name else ""


def artifact_prefix(source_sha256: str, config_hash: str, ocr: bool) -> str:
    """存档键前缀：converted/<原件sha256>/<config_hash>/<ocr|noocr>"""
    return f"converted/{source_sha256}/{config_hash}/{'ocr' if ocr else 'noocr'}"


class DoclingConverter:
    """Converter 端口的 Docling Serve 实现。

    存档布局（均在 BlobStore 中）：<prefix>/doc.md、<prefix>/doc.json、<prefix>/meta.json。
    meta.json 最后写入，它的存在表示该次转换已完整存档。
    """

    def __init__(self, base_url: str, blobs: BlobStore, *, parser_version: str, api_key: str | None = None,
                 timeout_seconds: float = 300.0, http: httpx.Client | None = None) -> None:
        self._url = base_url.rstrip("/") + "/v1/convert/file"
        self._blobs = blobs
        self._parser_version = parser_version
        self._headers = {"X-Api-Key": api_key} if api_key else {}
        self._timeout_seconds = timeout_seconds
        self._http = http if http is not None else httpx.Client(timeout=timeout_seconds)

    def _read_json(self, key: str) -> dict[str, Any]:
        """读取存档对象，损坏的存档作为永久错误暴露。"""
        try:
            value = json.loads(self._blobs.get(key))
        except (ValueError, UnicodeError) as exc:
            raise PermanentError(f"存档不是有效 JSON：{key}") from exc
        if not isinstance(value, dict):
            raise PermanentError(f"存档 JSON 不是对象：{key}")
        return value

    def _result(self, prefix: str, config_hash: str) -> ConvertResult:
        """元信息以已经完成的存档为准。"""
        meta = self._read_json(f"{prefix}/meta.json")
        try:
            return ConvertResult(
                f"{prefix}/doc.md", f"{prefix}/doc.json", meta["page_count"],
                meta["ocr_used"], meta["parser"], meta["parser_version"], config_hash,
            )
        except KeyError as exc:
            raise PermanentError("转换存档的元信息不完整") from exc

    def _store_first(self, key: str, data: bytes, content_type: str) -> None:
        """保留先写入的产物，包括检查后其他调用刚刚写入的情况。"""
        if self._blobs.exists(key):
            return
        try:
            self._blobs.put(key, data, content_type=content_type)
        except PermanentError:
            if not self._blobs.exists(key):
                raise

    @staticmethod
    def _error_detail(body: Any) -> str:
        """提取服务给出的错误信息，不包含请求凭据。"""
        if not isinstance(body, dict):
            return "响应不是 JSON 对象"
        messages = []
        errors = body.get("errors")
        if isinstance(errors, list):
            messages.extend(str(error["error_message"]) for error in errors
                            if isinstance(error, dict) and error.get("error_message"))
        for field in ("error_message", "detail"):
            if body.get(field):
                messages.append(str(body[field]))
        return "; ".join(messages) or f"status={body.get('status')!r}，返回内容不满足成功条件"

    def convert(self, source_key: str, *, filename: str, config_hash: str, ocr: bool) -> ConvertResult:
        if extension_of(filename) not in SUPPORTED_EXTENSIONS:
            raise UnsupportedFormatError(f"不支持的文件格式：{filename}")
        source = self._blobs.get(source_key)
        prefix = artifact_prefix(hashlib.sha256(source).hexdigest(), config_hash, ocr)
        if self._blobs.exists(f"{prefix}/meta.json"):
            return self._result(prefix, config_hash)

        try:
            response = self._http.post(
                self._url, headers=self._headers, timeout=self._timeout_seconds,
                files={"files": (filename, source, "application/octet-stream")},
                data={"to_formats": ["md", "json"], "do_ocr": "true" if ocr else "false"},
            )
        except httpx.RequestError as exc:
            raise TransientError(f"Docling 请求失败：{exc}") from exc
        try:
            body = response.json()
        except ValueError as exc:
            if 500 <= response.status_code < 600:
                raise TransientError(f"Docling HTTP {response.status_code}") from exc
            raise PermanentError(f"Docling HTTP {response.status_code}，响应不是有效 JSON") from exc
        if 500 <= response.status_code < 600:
            raise TransientError(f"Docling HTTP {response.status_code}：{self._error_detail(body)}")
        if response.status_code != 200:
            raise PermanentError(f"Docling HTTP {response.status_code}：{self._error_detail(body)}")
        document = body.get("document") if isinstance(body, dict) else None
        if (not isinstance(body, dict) or body.get("status") != "success" or body.get("errors") != []
                or not isinstance(document, dict) or not isinstance(document.get("md_content"), str)
                or not isinstance(document.get("json_content"), dict)):
            raise PermanentError(f"Docling 转换失败：{self._error_detail(body)}")

        self._store_first(f"{prefix}/doc.md", document["md_content"].encode("utf-8"), "text/markdown")
        self._store_first(f"{prefix}/doc.json", json.dumps(document["json_content"], ensure_ascii=False).encode("utf-8"),
                          "application/json")
        pages = self._read_json(f"{prefix}/doc.json").get("pages", {})
        if not isinstance(pages, dict):
            raise PermanentError("Docling 存档中的 pages 不是对象")
        meta = {"page_count": len(pages), "ocr_used": ocr, "parser": PARSER_NAME,
                "parser_version": self._parser_version}
        self._store_first(f"{prefix}/meta.json", json.dumps(meta, ensure_ascii=False).encode("utf-8"), "application/json")
        return self._result(prefix, config_hash)


