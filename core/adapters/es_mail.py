"""MailIndex 的 Elasticsearch 实现（批次 17a）。规格见 docs/spec-30-mail.md 第五节。

只用 httpx 调 REST 接口（不引入官方客户端）。映射与查询由下面的纯函数生成，便于离线检查。

分词（规格 30 第四节）：
- haina_bigram：standard 分词 → cjk_width（全角 ASCII 折为半角）→ lowercase → cjk_bigram（中日韩字符两两组词，孤立的单字保留）。
  关键词用 match_phrase 查询，在二元切分下等价于 core.domain.mail.term_matches 的连续出现规则（契约测试覆盖的情形）。
- haina_smart：smartcn_tokenizer（官方插件 analysis-smartcn），作为 subject.smart、body.smart 子字段，供 17b 相关性排序；
  17a 的检索不用它，但映射里要有，避免以后重建索引。
- 地址用 keyword + lowercase 规范化器，精确匹配。

权限：每条记录有 folder_tree（所在文件夹及其全部祖先，编码为 "/" + 路径，根为 "/"）。
FolderScope 的每个 (g, excluded) 变成 “folder_tree 含 g 且不含 excluded 中任一个”，各项之间为 OR；没有授权项 → match_none。
"""
from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from typing import Any

import httpx

from core.app.mail import RECIPIENTS_SHOWN, RELEVANT_LIMIT_MAX, THREAD_LIMIT_MAX, MailHit, MailPage
from core.app.ports import PermanentError, TransientError
from core.domain.folders import folder_ancestors
from core.domain.mail import Address, MailDoc, make_snippet
from core.domain.mail_qa import ThreadKey, question_keys
from core.domain.mail_query import TOTAL_CAP, MailQuery, check_page
from core.domain.permissions import FolderScope

MAPPING_VERSION = 1
BULK_BYTES = 20_000_000         # 单个 _bulk 请求体的上限（字节）；超过时拆成多个请求
_MARK_START, _MARK_END = "\u0002", "\u0003"     # 高亮标记（正文经 normalize_text 已去掉控制字符，不会与原文冲突）
_TEXT_FIELDS = ("subject", "body", "attachments.name", "sender.name", "to.name", "cc.name", "bcc.name")
_DERIVED = ("folder_tree", "attachment_count")


def encode_folder(path: str) -> str:
    """文件夹路径在 folder_tree 中的编码："/" + 路径；根 "" 为 "/"。"""
    return "/" + path


def folder_tree(folder: str) -> list[str]:
    """folder 及其全部祖先（含根）的编码，由浅到深。"""
    return [encode_folder("")] + [encode_folder(path) for path in folder_ancestors(folder)]


def index_body(*, shards: int = 1, replicas: int = 0) -> dict[str, Any]:
    """创建索引的请求体：settings（分析器）与严格映射（dynamic: strict），_meta.haina_mapping = MAPPING_VERSION。"""
    text = {"type": "text", "analyzer": "haina_bigram"}
    smart = {"type": "text", "analyzer": "haina_bigram", "fields": {"smart": {"type": "text", "analyzer": "haina_smart"}}}
    addr = {"type": "keyword", "normalizer": "haina_lower"}
    person = {"properties": {"name": text, "addr": addr}}
    return {
        "settings": {
            "number_of_shards": shards, "number_of_replicas": replicas,
            "analysis": {
                "analyzer": {
                    "haina_bigram": {"type": "custom", "tokenizer": "standard",
                                     "filter": ["cjk_width", "lowercase", "cjk_bigram"]},
                    "haina_smart": {"type": "custom", "tokenizer": "smartcn_tokenizer",
                                    "filter": ["cjk_width", "lowercase"]},
                },
                "normalizer": {"haina_lower": {"type": "custom", "filter": ["lowercase"]}},
            },
        },
        "mappings": {
            "dynamic": "strict",
            "_meta": {"haina_mapping": MAPPING_VERSION},
            "properties": {
                "mail_id": {"type": "keyword"}, "root": {"type": "keyword"}, "path": {"type": "keyword"},
                "folder": {"type": "keyword"}, "folder_tree": {"type": "keyword"}, "size": {"type": "long"},
                "message_id": {"type": "keyword"}, "subject": smart, "sender": person, "to": person, "cc": person,
                "bcc": person, "sent_at": {"type": "date", "format": "strict_date_optional_time"},
                "in_reply_to": {"type": "keyword"}, "references": {"type": "keyword"}, "body": smart,
                "body_truncated": {"type": "boolean"}, "body_kind": {"type": "keyword"},
                "attachments": {"properties": {"name": text, "content_type": {"type": "keyword"},
                                               "size": {"type": "long"}}},
                "attachment_count": {"type": "integer"}, "problems": {"type": "keyword"},
            },
        },
    }


def source_of(doc: MailDoc) -> dict[str, Any]:
    """写入的 _source：MailDoc.to_json() 加上派生字段 folder_tree 与 attachment_count。"""
    return {**doc.to_json(), "folder_tree": folder_tree(doc.folder), "attachment_count": len(doc.attachments)}


def doc_of(source: dict[str, Any]) -> MailDoc:
    return MailDoc.from_json({key: value for key, value in source.items() if key not in _DERIVED})


def _phrase_any(fields: Sequence[str], text: str) -> dict[str, Any]:
    return {"bool": {"should": [{"match_phrase": {field: text}} for field in fields], "minimum_should_match": 1}}


def _person(wanted: str, roles: Sequence[str]) -> dict[str, Any]:
    if "@" in wanted:
        return {"bool": {"should": [{"term": {f"{role}.addr": wanted}} for role in roles], "minimum_should_match": 1}}
    return _phrase_any([f"{role}.name" for role in roles], wanted)


def scope_filter(scope: FolderScope) -> dict[str, Any]:
    if not scope.grants:
        return {"match_none": {}}
    should = []
    for granted, excluded in scope.grants:
        clause: dict[str, Any] = {"filter": [{"term": {"folder_tree": encode_folder(granted)}}]}
        if excluded:
            clause["must_not"] = [{"terms": {"folder_tree": [encode_folder(path) for path in excluded]}}]
        should.append({"bool": clause})
    return {"bool": {"should": should, "minimum_should_match": 1}}


def query_filters(query: MailQuery, scope: FolderScope | None) -> list[dict[str, Any]]:
    """全部条件放在 bool.filter 中（不计分，结果按时间排序）。"""
    filters: list[dict[str, Any]] = [_phrase_any(_TEXT_FIELDS, term) for term in query.terms]
    if query.sender is not None:
        filters.append(_person(query.sender, ("sender",)))
    if query.recipient is not None:
        filters.append(_person(query.recipient, ("to", "cc", "bcc")))
    if query.start is not None or query.end is not None:
        bounds: dict[str, Any] = {"format": "yyyy-MM-dd", "time_zone": "+08:00"}
        if query.start is not None:
            bounds["gte"] = query.start.isoformat()
        if query.end is not None:
            bounds["lte"] = query.end.isoformat()
        filters.append({"range": {"sent_at": bounds}})
    if query.folder is not None:
        filters.append({"term": {"folder_tree": encode_folder(query.folder)}})
    if query.has_attachments is True:
        filters.append({"range": {"attachment_count": {"gte": 1}}})
    elif query.has_attachments is False:
        filters.append({"term": {"attachment_count": 0}})
    if scope is not None:
        filters.append(scope_filter(scope))
    return filters


def search_body(query: MailQuery, scope: FolderScope | None, *, offset: int, limit: int) -> dict[str, Any]:
    """_search 的请求体。排序：sent_at 降序（缺失的在最后）、mail_id 升序；track_total_hits 为 TOTAL_CAP；
    _source 只取结果行需要的字段；正文高亮取一个片段（no_match_size 保证没有命中时也给出开头）。"""
    body: dict[str, Any] = {
        "from": offset, "size": limit, "track_total_hits": TOTAL_CAP,
        "query": {"bool": {"filter": query_filters(query, scope)}},
        "sort": [{"sent_at": {"order": "desc", "missing": "_last"}}, {"mail_id": {"order": "asc"}}],
        "_source": _HIT_SOURCE,
        "highlight": {"fields": {"body": {"type": "unified", "number_of_fragments": 1, "fragment_size": 150,
                                          "no_match_size": 200, "pre_tags": [_MARK_START],
                                          "post_tags": [_MARK_END]}}},
    }
    if query.terms:
        body["highlight"]["fields"]["body"]["highlight_query"] = {
            "bool": {"should": [{"match_phrase": {"body": term}} for term in query.terms]}}
    return body


_HIT_SOURCE = ["mail_id", "folder", "subject", "sender", "to", "cc.addr", "bcc.addr", "sent_at", "size",
               "attachment_count", "message_id"]
_PREFIX_HIGHLIGHT = {"fields": {"body": {"type": "unified", "number_of_fragments": 1, "fragment_size": 150,
                                         "no_match_size": 200, "pre_tags": [_MARK_START], "post_tags": [_MARK_END]}}}


def relevant_body(text: str, query: MailQuery, scope: FolderScope | None, *, limit: int) -> dict[str, Any]:
    """相关度检索（批次 17b）的请求体：
    - 过滤条件同 search（query_filters）；
    - 必须命中：在 _TEXT_FIELDS 任一字段上 match（operator or，即至少一个二元词元）；
    - 计分：上述 match（subject 加权 2），另加 subject.smart、body.smart 的 match 作为 should；
    - 排序：_score 降序，然后 sent_at 降序（缺失在后）、mail_id 升序；返回完整 _source（问答需要正文）。"""
    fields = [("subject", 2.0)] + [(field, 1.0) for field in _TEXT_FIELDS if field != "subject"]
    return {
        "size": limit, "track_total_hits": False,
        "query": {"bool": {
            "filter": query_filters(query, scope),
            "must": [{"bool": {"should": [{"match": {field: {"query": text, "operator": "or", "boost": boost}}}
                                          for field, boost in fields], "minimum_should_match": 1}}],
            "should": [{"match": {"subject.smart": {"query": text}}}, {"match": {"body.smart": {"query": text}}}]}},
        "sort": ["_score", {"sent_at": {"order": "desc", "missing": "_last"}}, {"mail_id": {"order": "asc"}}],
    }


def thread_body(key: ThreadKey, scope: FolderScope | None, *, limit: int) -> dict[str, Any]:
    """会话线索（批次 17b）的请求体：message_id ∈ key.ids，或 references 含 key.root，或 in_reply_to == key.root；
    按 sent_at 升序（缺失在后）、mail_id 升序；摘要用正文开头（no_match_size）。"""
    filters: list[dict[str, Any]] = [{"bool": {"should": [
        {"terms": {"message_id": list(key.ids)}}, {"term": {"references": key.root}},
        {"term": {"in_reply_to": key.root}}], "minimum_should_match": 1}}]
    if scope is not None:
        filters.append(scope_filter(scope))
    return {"size": limit, "track_total_hits": False, "query": {"bool": {"filter": filters}},
            "sort": [{"sent_at": {"order": "asc", "missing": "_last"}}, {"mail_id": {"order": "asc"}}],
            "_source": _HIT_SOURCE, "highlight": _PREFIX_HIGHLIGHT}


def hit_of(hit: dict[str, Any], terms: Sequence[str]) -> MailHit:
    source = hit["_source"]
    fragments = hit.get("highlight", {}).get("body") or [""]
    fragment = fragments[0].replace(_MARK_START, "").replace(_MARK_END, "")
    to = [Address.from_json(item) for item in source.get("to") or []]
    recipients = len(to) + len(source.get("cc") or []) + len(source.get("bcc") or [])
    return MailHit(source["mail_id"], source["folder"], source["subject"],
                   Address.from_json(source["sender"]) if source.get("sender") else None,
                   tuple(to[:RECIPIENTS_SHOWN]), recipients, source.get("sent_at"), make_snippet(fragment, terms),
                   int(source.get("attachment_count") or 0), int(source["size"]), source.get("message_id"))


class EsMailIndex:
    """url：Elasticsearch 地址（如 http://127.0.0.1:9200）；index：索引名；user / password：基本认证（可省）；
    verify：TLS 校验（布尔或 CA 文件路径）。

    - 连接失败、超时、429、5xx → TransientError；其他非 2xx → PermanentError（消息含状态码与 ES 的错误类型，不含凭据）。
    - upsert：_bulk 的 index 操作，_id 为 mail_id，请求体超过 BULK_BYTES 时拆分；响应中有失败项 → 429/5xx 的失败 TransientError，
      否则 PermanentError（第一个失败的 mail_id 与原因）。
    - delete：_bulk 的 delete 操作，404 的项忽略。
    - get：GET _doc；404 → None；_source 去掉派生字段后还原为 MailDoc。
    - search：search_body；hits.total 的 relation 为 "eq" 时 total 准确，否则 total = TOTAL_CAP、total_exact = False。
    - ensure：索引不存在 → PUT index_body()；创建时报 resource_already_exists_exception（并发创建）→ 再检查版本；
      已存在 → GET _mapping 比较 _meta.haina_mapping，不同 → PermanentError。
    - drop()：删除索引（测试与清理用）；不存在时忽略。"""

    def __init__(self, url: str, index: str, *, user: str | None = None, password: str | None = None,
                 verify: bool | str = True, timeout_seconds: float = 60.0, shards: int = 1, replicas: int = 0,
                 http: httpx.Client | None = None) -> None:
        if not index or index != index.lower() or any(char in index for char in ' "*\\<|,>/?#:'):
            raise ValueError("Elasticsearch 索引名无效")
        self._url, self._index = url.rstrip("/"), index
        self._shards, self._replicas = shards, replicas
        auth = (user, password or "") if user else None
        self._http = http or httpx.Client(verify=verify, timeout=timeout_seconds)
        self._auth = auth

    @property
    def index(self) -> str:
        return self._index

    def _request(self, method: str, path: str, *, body: Any = None, content: bytes | None = None,
                 ok: Collection[int] = (200, 201)) -> httpx.Response:
        headers = {"Content-Type": "application/x-ndjson" if content is not None else "application/json"}
        try:
            response = self._http.request(method, f"{self._url}/{path.lstrip('/')}", auth=self._auth, headers=headers,
                                          content=content if content is not None else
                                          (json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None
                                           else None))
        except httpx.TimeoutException as exc:
            raise TransientError("Elasticsearch 请求超时") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"无法连接 Elasticsearch：{type(exc).__name__}") from exc
        if response.status_code in ok:
            return response
        kind = ""
        try:
            error = response.json().get("error")
            kind = error.get("type", "") if isinstance(error, dict) else str(error or "")
        except (ValueError, AttributeError):
            pass
        message = f"Elasticsearch 返回 {response.status_code} {kind}".strip()
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientError(message)
        raise PermanentError(message)

    def ensure(self) -> None:
        exists = self._request("HEAD", self._index, ok=(200, 404)).status_code == 200
        if not exists:
            try:
                self._request("PUT", self._index, body=index_body(shards=self._shards, replicas=self._replicas))
                return
            except PermanentError as exc:
                if "resource_already_exists_exception" not in str(exc):
                    raise
        mapping = self._request("GET", f"{self._index}/_mapping").json()
        meta = next(iter(mapping.values()), {}).get("mappings", {}).get("_meta", {})
        if meta.get("haina_mapping") != MAPPING_VERSION:
            raise PermanentError("邮件索引的映射版本与程序不一致，需要重建索引")

    def drop(self) -> None:
        self._request("DELETE", self._index, ok=(200, 404))

    def _bulk(self, lines: list[tuple[str, bytes]]) -> None:
        """lines：[(mail_id, 该条操作的 NDJSON 字节)]，按 BULK_BYTES 拆分发送。"""
        batch: list[tuple[str, bytes]] = []
        size = 0
        for item in lines:
            if batch and size + len(item[1]) > BULK_BYTES:
                self._send(batch)
                batch, size = [], 0
            batch.append(item)
            size += len(item[1])
        if batch:
            self._send(batch)

    def _send(self, batch: list[tuple[str, bytes]]) -> None:
        response = self._request("POST", "_bulk", content=b"".join(line for _, line in batch)).json()
        if not response.get("errors"):
            return
        transient = None
        for (mail_id, _), item in zip(batch, response.get("items", [])):
            result = next(iter(item.values()))
            status = result.get("status", 0)
            if status < 300 or ("delete" in item and status == 404):
                continue
            reason = (result.get("error") or {}).get("type", "") if isinstance(result.get("error"), dict) else ""
            if status == 429 or status >= 500:
                transient = transient or f"{mail_id}: {status} {reason}"
                continue
            raise PermanentError(f"邮件 {mail_id} 写入索引失败：{status} {reason}")
        if transient:
            raise TransientError(f"邮件写入索引暂时失败：{transient}")

    def upsert(self, docs: Sequence[MailDoc]) -> None:
        lines = []
        for doc in docs:
            action = json.dumps({"index": {"_index": self._index, "_id": doc.mail_id}})
            source = json.dumps(source_of(doc), ensure_ascii=False)
            lines.append((doc.mail_id, f"{action}\n{source}\n".encode("utf-8")))
        if lines:
            self._bulk(lines)

    def delete(self, mail_ids: Collection[str]) -> None:
        lines = [(mail_id, (json.dumps({"delete": {"_index": self._index, "_id": mail_id}}) + "\n").encode("utf-8"))
                 for mail_id in mail_ids]
        if lines:
            self._bulk(lines)

    def get(self, mail_id: str) -> MailDoc | None:
        response = self._request("GET", f"{self._index}/_doc/{mail_id}", ok=(200, 404))
        if response.status_code == 404:
            return None
        data = response.json()
        return doc_of(data["_source"]) if data.get("found") else None

    def search(self, query: MailQuery, *, scope: FolderScope | None, offset: int, limit: int) -> MailPage:
        check_page(offset, limit)
        data = self._request("POST", f"{self._index}/_search",
                             body=search_body(query, scope, offset=offset, limit=limit)).json()
        total = data["hits"]["total"]
        exact = total.get("relation") == "eq" and total["value"] <= TOTAL_CAP
        rows = tuple(hit_of(hit, query.terms) for hit in data["hits"]["hits"])
        return MailPage(rows, total["value"] if exact else TOTAL_CAP, exact)

    def relevant(self, text: str, query: MailQuery, *, scope: FolderScope | None, limit: int) -> list[MailDoc]:
        if not 1 <= limit <= RELEVANT_LIMIT_MAX:
            raise ValueError("候选数超出范围")
        if not question_keys(text):
            return []
        data = self._request("POST", f"{self._index}/_search",
                             body=relevant_body(text, query, scope, limit=limit)).json()
        return [doc_of(hit["_source"]) for hit in data["hits"]["hits"]]

    def thread(self, key: ThreadKey, *, scope: FolderScope | None, limit: int) -> list[MailHit]:
        if not 1 <= limit <= THREAD_LIMIT_MAX:
            raise ValueError("会话记录数超出范围")
        data = self._request("POST", f"{self._index}/_search", body=thread_body(key, scope, limit=limit)).json()
        return [hit_of(hit, ()) for hit in data["hits"]["hits"]]

    def count(self) -> int:
        return int(self._request("GET", f"{self._index}/_count").json()["count"])

    def refresh(self) -> None:
        self._request("POST", f"{self._index}/_refresh")
