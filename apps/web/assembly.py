"""启动装配（批次 15c）：Web 进程与 worker 进程共用。服务凭据只从进程环境读取。"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from apps.web.jobs import china_now
from apps.web.services import Redactor, Services
from apps.web.store import KbStore
from core.adapters.docling_http import DoclingConverter
from core.adapters.fs_blob import FileSystemBlobStore
from core.adapters.gpustack_llm import GpustackLlm
from core.adapters.ragflow_http import RagflowDatasets, RagflowIndexer
from core.app.ports import PermanentError, Role, TransientError
from core.domain.model_location import LocationReport, check_locations, parse_hosts, parse_profile
from apps.runtime import DEFAULT_CHAT_MODEL
from apps.runtime import PARSER_VERSION

REQUIRED = ("DOCLING_URL", "RAGFLOW_URL", "RAGFLOW_API_KEY", "GPUSTACK_URL")
SECRET_NAMES = ("RAGFLOW_API_KEY", "GPUSTACK_API_KEY", "DOCLING_API_KEY", "HAINA_PG_DSN", "HAINA_ES_PASSWORD")


def missing_environment() -> list[str]:
    return [name for name in REQUIRED if not os.environ.get(name)]


SERVICE_URLS = ("GPUSTACK_URL", "RAGFLOW_URL", "DOCLING_URL", "HAINA_ES_URL")


def location_from_environment() -> LocationReport:
    """批次 21a：按 HAINA_DATA_PROFILE（缺省 sample）与 HAINA_LOCAL_HOSTS 判断各服务地址是否本地。
    档位取值不合规 → ValueError。调用方：report.problems() 非空时拒绝启动；model_location 交给界面显示。"""
    return check_locations(parse_profile(os.environ.get("HAINA_DATA_PROFILE")),
                           {name: os.environ.get(name) for name in SERVICE_URLS},
                           parse_hosts(os.environ.get("HAINA_LOCAL_HOSTS")))


def location_problems() -> tuple[list[str], str]:
    """(启动时应打印并拒绝启动的错误, model_location)。档位不合规时错误为该条说明。"""
    try:
        report = location_from_environment()
    except ValueError as exc:
        return [str(exc)], "online"
    return report.problems(), report.model_location


def check_embedding(dataset_id: str, http: httpx.Client) -> None:
    """批次 21b：正式数据档位下，当前检索数据集的向量模型必须登记在 HAINA_LOCAL_EMBEDDING_MODELS（逗号分隔，不区分大小写）中。
    RAGFlow 内部调用向量模型，地址检查（location_problems）看不到它。样例档位不检查，不发请求。
    不合规、数据集不存在或读不到 → ValueError（启动时打印并以退出码 2 结束）。"""
    if parse_profile(os.environ.get("HAINA_DATA_PROFILE")) != "formal":
        return
    allowed = parse_hosts(os.environ.get("HAINA_LOCAL_EMBEDDING_MODELS"))
    try:
        info = RagflowDatasets(os.environ["RAGFLOW_URL"], os.environ["RAGFLOW_API_KEY"], http=http).info(dataset_id)
    except (TransientError, PermanentError) as exc:
        raise ValueError(f"无法确认检索数据集的向量模型：{exc}") from exc
    if info is None:
        raise ValueError("检索数据集不存在，无法确认其向量模型")
    if info.embedding_model.strip().lower() not in allowed:
        raise ValueError(f"正式数据档位只允许本地向量模型：当前检索数据集使用 {info.embedding_model or '（未知）'}"
                         "（本地部署的向量模型请登记在 HAINA_LOCAL_EMBEDDING_MODELS 中）")


def redactor_from_environment() -> tuple[tuple[str, ...], Redactor]:
    secrets = tuple(os.environ.get(name, "") for name in SECRET_NAMES)
    return secrets, Redactor(secrets)


def open_stores(data_root: Path, *, seed_from: str | None = None) -> dict[str, Any]:
    """{"store", "data_root", 以及数据库模式下的 "knowledge","links","decisions","queries","tasks","scan_index","conversations"（批次 19）,"rebuilds"（批次 21b）,
    "identity","permission_repo"（批次 16a）}。identity / permission_repo 由调用方取出后用 account_parts 组装，不直接传给 create_app。

    设置了 HAINA_PG_DSN 时：文档登记、知识数据、任务队列都在 PostgreSQL（HAINA_PG_SCHEMA，缺省 public），
    启动时执行幂等的结构迁移；--data 目录只存原件。否则为 JSON 登记处，任务队列在进程内（不能与其他进程共用）。"""
    parts: dict[str, Any] = {"data_root": data_root}
    dsn = os.environ.get("HAINA_PG_DSN")
    if dsn:
        from core.adapters.pg_knowledge import open_knowledge
        from core.adapters.pg_registry import PgRegistry, migrate
        from core.adapters.pg_identity import PgIdentityRepo, PgPermissionRepo
        from core.adapters.pg_conversations import PgConversationRepo
        from core.adapters.pg_reindex import PgRebuildRepo
        from core.adapters.pg_scan import PgScanIndex
        from core.adapters.pg_tasks import PgTaskRepo
        from core.app.query_cache import CachedQueries
        schema = os.environ.get("HAINA_PG_SCHEMA") or "public"
        migrate(dsn, schema=schema)
        store = PgRegistry(dsn, schema=schema)
        _, knowledge, links, decisions, queries = open_knowledge(dsn, schema=schema)
        parts.update(store=store, knowledge=knowledge, links=links, decisions=decisions,
                     queries=CachedQueries(queries, store.last_event_seq),
                     tasks=PgTaskRepo(dsn, schema=schema, clock=china_now),
                     scan_index=PgScanIndex(dsn, schema=schema), conversations=PgConversationRepo(dsn, schema=schema),
                     rebuilds=PgRebuildRepo(dsn, schema=schema),
                     identity=PgIdentityRepo(dsn, schema=schema),
                     permission_repo=PgPermissionRepo(dsn, schema=schema))
        data_root.mkdir(parents=True, exist_ok=True)
    else:
        parts["store"] = KbStore(data_root, seed_from=seed_from)
    return parts


def build_services(store: Any, data_root: Path, http: httpx.Client, *,
                   llm_factory: Callable[..., Any] = GpustackLlm) -> Services:
    """llm_factory：构造模型客户端（默认 GpustackLlm，测试可替换）。"""
    extra = json.loads(os.environ["GPUSTACK_EXTRA_BODY"]) if os.environ.get("GPUSTACK_EXTRA_BODY") else None
    if extra is not None and not isinstance(extra, dict):
        raise ValueError("GPUSTACK_EXTRA_BODY 必须是 JSON 对象")
    blobs = FileSystemBlobStore(data_root / "blobs")
    converter = DoclingConverter(os.environ["DOCLING_URL"], blobs, parser_version=PARSER_VERSION,
                                 api_key=os.environ.get("DOCLING_API_KEY") or None, timeout_seconds=600, http=http)
    indexer = RagflowIndexer(os.environ["RAGFLOW_URL"], os.environ["RAGFLOW_API_KEY"], store.dataset_id,
                             timeout_seconds=120, http=http)
    chat = os.environ.get("GPUSTACK_CHAT_MODEL") or DEFAULT_CHAT_MODEL
    # 批次 17b：GPUSTACK_RERANK_MODEL 为重排模型（问邮件时使用；未设置时按检索顺序，不调用重排）。
    rerank = {"rerank_model": os.environ["GPUSTACK_RERANK_MODEL"]} if os.environ.get("GPUSTACK_RERANK_MODEL") else {}
    llm = llm_factory(os.environ["GPUSTACK_URL"],
                      models={Role.QA: chat, Role.EXTRACT: os.environ.get("GPUSTACK_EXTRACT_MODEL") or chat,
                              Role.REVIEW: os.environ.get("GPUSTACK_REVIEW_MODEL") or chat,
                              # 批次 21a：多轮改写与关键词抽取，缺省同问答模型。
                              Role.REWRITE: os.environ.get("GPUSTACK_REWRITE_MODEL") or chat}, **rerank,
                      api_key=os.environ.get("GPUSTACK_API_KEY") or None, extra_body=extra,
                      structured_output=os.environ.get("GPUSTACK_STRUCTURED") != "0", http=http)
    def indexer_for(dataset_id: str) -> RagflowIndexer:                 # 批次 21b
        return RagflowIndexer(os.environ["RAGFLOW_URL"], os.environ["RAGFLOW_API_KEY"], dataset_id,
                              timeout_seconds=120, http=http)

    return Services(blobs, converter, indexer, llm, indexer_for=indexer_for)


def scan_settings() -> tuple[dict[str, Path], int | None]:
    """从环境读取扫描配置（批次 15d）：(HAINA_SCAN_ROOTS 解析结果, HAINA_SCAN_INTERVAL_MINUTES)。不合规 → ValueError。"""
    from apps.web.scan import parse_scan_interval, parse_scan_roots
    return (parse_scan_roots(os.environ.get("HAINA_SCAN_ROOTS")),
            parse_scan_interval(os.environ.get("HAINA_SCAN_INTERVAL_MINUTES")))


def account_parts(store: Any, identity: Any, permission_repo: Any) -> tuple[Any, Any]:
    """组装账号模式（批次 16a）：返回 (AuthService, PermissionService)。"""
    from core.adapters.passwords import ScryptHasher
    from core.app.identity import AuthService
    from core.app.permissions import PermissionService
    auth = AuthService(identity, ScryptHasher(), clock=china_now)
    permissions = PermissionService(store, permission_repo, known_groups=auth.group_ids, clock=china_now)
    return auth, permissions


def mail_parts_from_environment(parts: dict[str, Any]) -> Any:
    """批次 17a：由环境组装邮件部件（apps.web.mail.MailParts），未设置 HAINA_ES_URL 时为 None（不启用邮件）。

    - HAINA_ES_URL、HAINA_ES_INDEX（缺省 "haina_mail"）、HAINA_ES_USER、HAINA_ES_PASSWORD、HAINA_ES_CA（CA 文件路径，缺省按系统证书校验）、
      HAINA_ES_SHARDS（新建索引的主分片数，缺省 1）；
    - HAINA_MAIL_ROOTS、HAINA_MAIL_SCAN_INTERVAL_MINUTES（解析与校验见 apps.web.mail.parse_mail_settings，
      名称不得与 HAINA_SCAN_ROOTS 重名）；
    - 邮件只支持数据库模式：parts 中没有 "tasks"（未设置 HAINA_PG_DSN）而设置了 HAINA_ES_URL → ValueError；
      设置了 HAINA_MAIL_ROOTS 而没有 HAINA_ES_URL → ValueError。"""
    from apps.web.mail import MailParts, parse_mail_settings
    url = os.environ.get("HAINA_ES_URL")
    if not url:
        from apps.web.scan import parse_scan_roots
        if parse_scan_roots(os.environ.get("HAINA_MAIL_ROOTS"), variable="HAINA_MAIL_ROOTS"):
            raise ValueError("设置了 HAINA_MAIL_ROOTS，但没有设置 HAINA_ES_URL")
        return None
    if "tasks" not in parts:
        raise ValueError("邮件只支持数据库模式，请设置 HAINA_PG_DSN")
    from core.adapters.es_mail import EsMailIndex
    from core.adapters.pg_mail import PgMailCatalog
    try:
        shards = int(os.environ.get("HAINA_ES_SHARDS") or 1)
    except ValueError as exc:
        raise ValueError("HAINA_ES_SHARDS 必须是整数") from exc
    if not 1 <= shards <= 64:
        raise ValueError("HAINA_ES_SHARDS 取 1–64")
    document_roots, _ = scan_settings()
    roots, interval = parse_mail_settings(os.environ.get("HAINA_MAIL_ROOTS"),
                                          os.environ.get("HAINA_MAIL_SCAN_INTERVAL_MINUTES"),
                                          document_roots=document_roots)
    index = EsMailIndex(url, os.environ.get("HAINA_ES_INDEX") or "haina_mail",
                        user=os.environ.get("HAINA_ES_USER") or None, password=os.environ.get("HAINA_ES_PASSWORD") or None,
                        verify=os.environ.get("HAINA_ES_CA") or True, shards=shards)
    catalog = PgMailCatalog(os.environ["HAINA_PG_DSN"], schema=os.environ.get("HAINA_PG_SCHEMA") or "public")
    return MailParts(index, catalog, roots, interval)
