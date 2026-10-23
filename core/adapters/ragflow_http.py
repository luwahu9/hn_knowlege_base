"""RAGFlow 0.27.2 适配器（Indexer 端口）。规格：docs/spec-10-adapters.md 第二部分。"""
from __future__ import annotations

import json
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from threading import Event
from typing import Any
from urllib.parse import quote

import httpx

from core.app.ports import Chunk, Hit, IndexReceipt, PermanentError, TransientError, VerifyResult
from core.app.reindex import DatasetInfo

GROUPS_FIELD = "groups"           # 文档级元数据：形如 "|research|ops|"
VERSION_FIELD = "haina_version"
DOC_SUFFIX = ".md"
SAMPLE_QUERY_CHARS = 200
TRANSIENT_BODY_CODES = frozenset({106})   # “Operation still running”
_DOCUMENT_PAGE_SIZE = 100
_MISSING_DOCUMENT = object()
DEFAULT_PUBLISH_WORKERS = 4
MAX_PUBLISH_WORKERS = 32


def encode_groups(group_tags: Sequence[str]) -> str:
    """校验并编码用户组：去重排序后用 "|" 包围连接，例如 ["ops","research"] → "|ops|research|"。
    任一标签为空串或包含 "|" → ValueError；空列表编码为 "||"（不匹配任何检索条件）。"""
    if any(not isinstance(tag, str) or not tag or "|" in tag for tag in group_tags):
        raise ValueError("用户组标签必须是非空字符串，且不得包含 |")
    return "|" + "|".join(sorted(set(group_tags))) + "|"


class RagflowIndexer:
    """Indexer 端口的 RAGFlow 实现。一个实例绑定一个数据集；每个版本对应数据集中名为 "<version_id>.md" 的文档。"""

    def __init__(self, base_url: str, api_key: str, dataset_id: str, *, timeout_seconds: float = 60.0,
                 http: httpx.Client | None = None, publish_workers: int = DEFAULT_PUBLISH_WORKERS) -> None:
        """publish_workers：发布时并发写入分块的线程数，整数 1～MAX_PUBLISH_WORKERS（布尔值不算），否则 ValueError。
        1 表示按顺序逐个写入。"""
        if (type(publish_workers) is not int or not 1 <= publish_workers <= MAX_PUBLISH_WORKERS):
            raise ValueError(f"publish_workers 必须是 1 到 {MAX_PUBLISH_WORKERS} 的整数")
        self._publish_workers = publish_workers
        self._base_url = base_url.rstrip("/") + "/api/v1"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._dataset_id = dataset_id
        self._documents_path = f"/datasets/{quote(dataset_id, safe='')}/documents"
        self._timeout_seconds = timeout_seconds
        self._http = http if http is not None else httpx.Client(timeout=timeout_seconds)

    # ---------------- HTTP ----------------

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                 body: Any = None) -> Any:
        """所有请求走注入的客户端，统一区分传输错误与服务端业务错误。"""
        try:
            response = self._http.request(method, self._base_url + path, headers=self._headers,
                                          params=params, json=body, timeout=self._timeout_seconds)
        except httpx.RequestError as exc:
            raise TransientError(f"RAGFlow 请求失败：{exc}") from exc
        if 500 <= response.status_code < 600:
            raise TransientError(f"RAGFlow HTTP {response.status_code}")
        if not 200 <= response.status_code < 300:
            raise PermanentError(f"RAGFlow HTTP {response.status_code}")
        try:
            payload = json.loads(response.content)
        except (ValueError, UnicodeError) as exc:
            raise PermanentError("RAGFlow 响应不是有效 JSON") from exc
        payload = self._object(payload)
        code = payload.get("code")
        if type(code) is not int:
            raise PermanentError("RAGFlow 响应缺少有效业务码")
        if (code == 102 and method == "GET" and path == self._documents_path
                and params is not None and "name" in params):
            # 102 也可能表示无权访问数据集，先用一次轻量请求确认权限。
            self._request("GET", path, params={"page": 1, "page_size": 1})
            return _MISSING_DOCUMENT
        if code in TRANSIENT_BODY_CODES:
            raise TransientError(f"RAGFlow 业务码 {code}：{payload.get('message', '')}")
        if code != 0:
            raise PermanentError(f"RAGFlow 业务码 {code}：{payload.get('message', '')}")
        return payload.get("data")

    @staticmethod
    def _object(value: Any) -> dict[str, Any]:
        """拒绝结构不符合契约的响应。"""
        if not isinstance(value, dict):
            raise PermanentError("RAGFlow 响应中的数据不是对象")
        return value

    @staticmethod
    def _string(value: dict[str, Any], field: str) -> str:
        """读取响应中必需的字符串字段。"""
        result = value.get(field)
        if not isinstance(result, str) or not result:
            raise PermanentError(f"RAGFlow 响应缺少有效的 {field}")
        return result

    def _items(self, value: Any, field: str) -> list[dict[str, Any]]:
        """读取对象列表；不把缺失字段误判为空结果。"""
        items = self._object(value).get(field)
        if not isinstance(items, list):
            raise PermanentError(f"RAGFlow 响应中的 {field} 不是数组")
        return [self._object(item) for item in items]

    def _document_path(self, document_id: str) -> str:
        return self._documents_path + "/" + quote(document_id, safe="")

    def _find_document(self, version_id: str) -> dict[str, Any] | None:
        """遍历名称查询的结果，在本地精确匹配并拒绝重复版本。"""
        name = version_id + DOC_SUFFIX
        page = 1
        seen: set[str] = set()
        matches: dict[str, dict[str, Any]] = {}
        while True:
            result = self._request("GET", self._documents_path,
                params={"name": name, "page": page, "page_size": _DOCUMENT_PAGE_SIZE})
            if result is _MISSING_DOCUMENT:
                return None
            data = self._object(result)
            documents = self._items(data, "docs")
            ids = {self._string(document, "id") for document in documents}
            for document in documents:
                if document.get("name") == name:
                    matches[document["id"]] = document
            if len(matches) > 1:
                raise PermanentError(f"版本 {version_id} 存在多个同名文档")
            if documents and ids <= seen:
                raise PermanentError("RAGFlow 文档分页重复，无法确认版本唯一性")
            seen.update(ids)
            total = data.get("total")
            if (not documents or (isinstance(total, int) and len(seen) >= total)
                    or (total is None and len(documents) < _DOCUMENT_PAGE_SIZE)):
                return next(iter(matches.values()), None)
            page += 1

    def _add_chunk(self, path: str, text: str) -> str:
        added = self._object(self._request("POST", path + "/chunks", body={"content": text}))
        return self._string(self._object(added.get("chunk")), "id")

    def _add_chunks(self, path: str, texts: Sequence[str]) -> list[str]:
        """写入全部分块，返回与 texts 顺序一致的引擎分块 ID。

        publish_workers 为 1 时逐个顺序写入，遇错立即抛出。大于 1 时用线程池并发写入（线程数取 publish_workers 与分块数的较小值），
        请求按分块顺序提交；任一分块失败后不再开始新的分块请求（已开始的等待其结束），
        抛出失败分块中序号最小的那个异常。"""
        if self._publish_workers == 1 or len(texts) <= 1:
            return [self._add_chunk(path, text) for text in texts]

        stopped = Event()

        def _write(text: str) -> str | None:
            # 在线程内检查，避免主线程尚未收到失败通知时继续发起排队请求。
            if stopped.is_set():
                return None
            try:
                return self._add_chunk(path, text)
            except BaseException:
                stopped.set()
                raise

        futures: list[Future[str | None]] = []
        with ThreadPoolExecutor(max_workers=min(self._publish_workers, len(texts))) as executor:
            for text in texts:
                if stopped.is_set():
                    break
                futures.append(executor.submit(_write, text))
            for future in as_completed(futures):
                if future.exception() is not None:
                    for pending in futures:
                        pending.cancel()
                    break

        # 退出线程池后，在途请求均已结束；按提交顺序读取，以抛出最小序号的失败。
        engine_ids: list[str] = []
        for future in futures:
            if not future.cancelled():
                engine_id = future.result()
                if engine_id is not None:
                    engine_ids.append(engine_id)
        return engine_ids

    def _delete_document(self, document_id: str) -> None:
        self._request("DELETE", self._documents_path, body={"ids": [document_id]})

    # ---------------- 端口方法 ----------------
    def publish(self, version_id: str, chunks: Sequence[Chunk], group_tags: Sequence[str]) -> IndexReceipt:
        encoded = encode_groups(group_tags)
        if not chunks or len({chunk.chunk_id for chunk in chunks}) != len(chunks):
            raise ValueError("分块不得为空，chunk_id 不得重复")
        previous = self._find_document(version_id)
        if previous is not None:
            self._delete_document(self._string(previous, "id"))
        created = self._request("POST", self._documents_path, params={"type": "empty"},
                                body={"name": version_id + DOC_SUFFIX})
        if isinstance(created, list):
            if len(created) != 1:
                raise PermanentError("RAGFlow 创建文档的响应必须恰好包含一个文档")
            created = created[0]
        document_id = self._string(self._object(created), "id")
        path = self._document_path(document_id)
        engine_ids = self._add_chunks(path, [chunk.text for chunk in chunks])
        chunk_map = {chunk.chunk_id: engine_id for chunk, engine_id in zip(chunks, engine_ids)}
        # 全部分块写入成功后才开放检索权限，失败残留由下一次发布删除重建。
        self._request("PATCH", path, body={"meta_fields": {VERSION_FIELD: version_id, GROUPS_FIELD: encoded}})
        return IndexReceipt(version_id, document_id, len(chunks), chunk_map)

    def verify(self, version_id: str) -> VerifyResult:
        document = self._find_document(version_id)
        if document is None:
            return VerifyResult(False, 0, False)
        count = document.get("chunk_count")
        if type(count) is not int or count < 0:
            raise PermanentError("RAGFlow 响应缺少有效的 chunk_count")
        if count == 0:
            return VerifyResult(False, 0, False)
        document_id = self._string(document, "id")
        data = self._request("GET", self._document_path(document_id) + "/chunks",
                             params={"page": 1, "page_size": 1})
        chunks = self._items(data, "chunks")
        if not chunks:
            return VerifyResult(True, count, False)
        sample = self._string(chunks[0], "content")[:SAMPLE_QUERY_CHARS]
        data = self._request("POST", "/retrieval", body={
            "question": sample, "dataset_ids": [self._dataset_id], "document_ids": [document_id],
            "page": 1, "page_size": 1,
        })
        sample_hit = any(hit.get("document_id") == document_id for hit in self._items(data, "chunks"))
        return VerifyResult(True, count, sample_hit)

    def set_group_tags(self, version_ids: Sequence[str], group_tags: Sequence[str]) -> None:
        encoded = encode_groups(group_tags)
        documents = []
        for version_id in dict.fromkeys(version_ids):
            document = self._find_document(version_id)
            if document is None:
                raise PermanentError(f"版本 {version_id} 的引擎文档不存在")
            documents.append((version_id, document))
        for version_id, document in documents:
            metadata = dict(self._object(document.get("meta_fields", {})))
            metadata[GROUPS_FIELD] = encoded
            metadata[VERSION_FIELD] = version_id
            self._request("PATCH", self._document_path(self._string(document, "id")),
                          body={"meta_fields": metadata})

    def search(self, query: str, *, group_tags: Sequence[str], top_k: int) -> list[Hit]:
        encode_groups(group_tags)
        if not group_tags:
            return []
        data = self._request("POST", "/retrieval", body={
            "question": query, "dataset_ids": [self._dataset_id], "page": 1, "page_size": top_k,
            "metadata_condition": {"logic": "or", "conditions": [
                {"name": GROUPS_FIELD, "comparison_operator": "contains", "value": f"|{tag}|"}
                for tag in sorted(set(group_tags))
            ]},
        })
        hits = []
        for item in self._items(data, "chunks"):
            score = item.get("similarity")
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                raise PermanentError("RAGFlow 响应缺少有效的 similarity")
            hits.append(Hit(
                self._string(item, "document_keyword").removesuffix(DOC_SUFFIX),
                self._string(item, "id"), self._string(item, "content"), float(score), None, None, None,
            ))
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:top_k]

    def remove(self, version_id: str) -> None:
        document = self._find_document(version_id)
        if document is not None:
            self._delete_document(self._string(document, "id"))


class RagflowDatasets:
    """DatasetCatalog 的 RAGFlow 实现（批次 21b）。
    info：GET /api/v1/datasets?id=<id>&page=1&page_size=1；data 为数组（或含 "datasets" 数组的对象），取 id 相同的一项，
    读 name、embedding_model、document_count、chunk_count；没有匹配或业务码 102（数据集不存在或无权访问）→ None。
    delete：DELETE /api/v1/datasets，body {"ids": [id]}；随后 info 应为 None，否则 PermanentError。"""

    def __init__(self, base_url: str, api_key: str, *, timeout_seconds: float = 60.0,
                 http: httpx.Client | None = None) -> None:
        self._client = RagflowIndexer(base_url, api_key, "_", timeout_seconds=timeout_seconds, http=http)

    def info(self, dataset_id: str) -> DatasetInfo | None:
        try:
            data = self._client._request("GET", "/datasets", params={"id": dataset_id, "page": 1, "page_size": 1})
        except PermanentError as exc:
            if "业务码 102" in str(exc):
                return None
            raise
        items = data.get("datasets") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise PermanentError("RAGFlow 数据集列表响应格式不符")
        for item in items:
            item = RagflowIndexer._object(item)
            if item.get("id") != dataset_id:
                continue
            counts = [item.get("document_count"), item.get("chunk_count")]
            if any(type(c) is not int or c < 0 for c in counts):
                raise PermanentError("RAGFlow 响应缺少有效的文档数或分块数")
            return DatasetInfo(dataset_id, RagflowIndexer._string(item, "name"),
                               str(item.get("embedding_model") or ""), counts[0], counts[1])
        return None

    def delete(self, dataset_id: str) -> None:
        if self.info(dataset_id) is None:
            return
        self._client._request("DELETE", "/datasets", body={"ids": [dataset_id]})
        if self.info(dataset_id) is not None:
            raise PermanentError("删除后数据集仍然存在")
