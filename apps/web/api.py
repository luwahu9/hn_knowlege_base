"""FastAPI 接口与静态页面；业务处理复用 core 用例。"""
from __future__ import annotations

import re
import tempfile
import time
from collections.abc import Callable, Mapping
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path, PureWindowsPath
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool, StrictInt, StrictStr, field_validator
from starlette.exceptions import HTTPException

from apps.web.account_api import register as register_account_routes
from apps.web.acl_sync import AclSyncJobs
from apps.web.auth import (COOKIE, CSRF_HEADER, PUBLIC_PATHS, Access, AccountGate, FilteringIndexer, OpenGate,
                           action_for)
from apps.web.jobs import DuplicateUpload, JobManager, upload_name_error
from core.app.permissions import LegacyTagger, PermissionTagger
from core.app.registry import PAGE_LIMIT_MAX
from core.domain.permissions import Visibility
from apps.web.assistant import RegistryDocuments
from core.app.link_targets import LinkTargets
from apps.web.link_views import LinkViews
from core.app.lifecycle import LifecycleService
from core.app.purge import PurgeService
from core.app.reindex import RebuildRepo, forget_everywhere
from core.domain.lifecycle import same_document, version_groups
from apps.web.conversations import ConversationStore
from core.adapters.memory import InMemoryConversationRepo
from core.app.conversation import ConversationRepo
from core.domain.conversation import (MAX_TITLE_CHARS, MAX_TURNS, Conversation, ConversationSummary, HistoryTurn,
                                      history_of, new_conversation_id, title_of, turn_record, valid_conversation_id,
                                      visible_turn)
from apps.web.mail import FOLDER_DEPTH as MAIL_FOLDER_DEPTH
from apps.web.mail import MailJobs, MailParts
from apps.web.mail import register as register_mail_routes
from apps.web.scan import JsonScanIndex, ScanBusy, ScanJobs
from apps.web.uploads import OffsetMismatch, UploadStaging
from core.adapters.memory import InMemoryScanIndex
from core.app.scan import ScanIndex
from core.domain.scan import split_import_path
from apps.web.knowledge import ExtractionJobs, KnowledgeStore
from apps.web.links import LinkJobs, LinkStore
from apps.web.review import DecisionStore, ReviewedKnowledge
from core.app.knowledge import DecisionRepo, FullScanQueries, KnowledgeQueries, KnowledgeRepo, LinkRepo
from core.app.link import LinkService
from core.domain.relations import document_edges
from core.app.series import SeriesService
from core.domain.review import SERIES_KINDS, DecisionKind, ReviewError, make_decision, pending_questions
from apps.web.services import Redactor, Services
from apps.web.store import now_iso
from core.adapters.memory import InMemoryTaskRepo
from core.app.ports import TaskQueue, TaskStatus
from apps.web.jobs import china_now
from core.app.registry import (DECISION_CHANGED, DOCUMENT_PUBLISHED, EXTRACTION_DONE, DocumentRegistry,
                               EventDispatcher)
from core.adapters.docling_http import SUPPORTED_EXTENSIONS
from core.app.ports import PermanentError, TransientError
from core.app.extract import ExtractionService
from core.app.cross_qa import CrossQaService, NameDirectory
from core.app.qa import QaService
from core.app.retrieval import resolve_hits
from core.domain.qa import strip_breadcrumb
from core.domain.knowledge import DETAIL_LIMIT, TIMELINE_LIMIT_MAX, build_graph


LINK_PAGE = 200            # 批次 18c：/api/links 每类缺省返回的行数
LINK_PAGE_MAX = 2000
REVIEW_LINKS = 200         # /api/review 中待确认引用的最多条数
REVIEW_VERSIONS = 100      # 批次 20a：/api/review 中版本待确认的最多组数

class _TextRequest(BaseModel):
    @field_validator("*", mode="after")
    @classmethod
    def _trim(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            if not value or len(value) > 500:
                raise ValueError("请输入 1 至 500 字")
        return value


class _SearchRequest(_TextRequest):
    query: StrictStr
    top_k: StrictInt = Field(default=8, ge=1, le=20)


class _AskRequest(_TextRequest):
    question: StrictStr


class _AssistantRequest(_TextRequest):
    question: StrictStr
    mode: StrictStr = "all"
    conversation_id: StrictStr | None = None      # 批次 19：继续已有对话；缺省为新建对话


class _RenameRequest(BaseModel):
    title: StrictStr


class _WithdrawRequest(BaseModel):
    reason: StrictStr = ""


class _PurgeRequest(BaseModel):
    confirm: StrictStr                     # 批次 20b：必须等于版本号，防止误删


class _ExtractRequest(BaseModel):
    version_id: StrictStr | None = None
    force: StrictBool = False


class _DocumentReviewRequest(BaseModel):
    action: StrictStr
    note: StrictStr | None = Field(default=None, max_length=500)


def needs_review(row: dict[str, Any]) -> bool:
    """文档是否出现在审核列表（批次 15c）：未发布（重复文档除外）或有质检警告，且没有人工审核记录（review）。"""
    if row.get("review"):
        return False
    return row["status"] not in ("published", "duplicate") or any(
        finding["severity"] == "warning" for finding in row["qc"])


class _UploadItem(BaseModel):
    path: StrictStr = Field(min_length=1, max_length=4096)
    size: StrictInt
    sha256: StrictStr = Field(min_length=1, max_length=64)


class _UploadCheckRequest(BaseModel):
    files: list[_UploadItem] = Field(min_length=1, max_length=1000)


class _ScanRequest(BaseModel):
    root: StrictStr = Field(min_length=1, max_length=255)


class _DecisionRequest(BaseModel):
    kind: StrictStr
    target: StrictStr = Field(min_length=1, max_length=1000)
    value: StrictStr | None = Field(default=None, max_length=100)


def _recent(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(documents, key=lambda row: datetime.fromisoformat(row["imported_at"]).timestamp(), reverse=True)


def create_app(store: DocumentRegistry, services: Services, *, secrets: tuple[str, ...] = (),
               max_upload_bytes: int = 200_000_000, retry_delay: float = 5.0,
               auto_extract: bool = True, auto_links: bool = False,
               data_root: str | Path | None = None, event_poll_seconds: float = 1.0,
               knowledge: KnowledgeRepo | None = None, links: LinkRepo | None = None,
               decisions: DecisionRepo | None = None, queries: KnowledgeQueries | None = None,
               tasks: TaskQueue | None = None, run_workers: bool = True, worker_id: str = "web",
               worker_poll_seconds: float = 1.0, scan_roots: Mapping[str, Path] | None = None,
               scan_index: ScanIndex | None = None, scan_interval_minutes: int | None = None,
               staging_dir: str | Path | None = None, gate: OpenGate | AccountGate | None = None,
               secure_cookie: bool = False, mail: MailParts | None = None,
               conversations: ConversationRepo | None = None, model_location: str = "local",
               rebuilds: RebuildRepo | None = None) -> FastAPI:
    """store：文档登记处（KbStore 或 PgRegistry）。
    tasks：导入、抽取、连线三类作业共用的任务队列（批次 15c）；缺省为进程内的 InMemoryTaskRepo。
    run_workers：是否在本进程执行作业；为 False 时只入队，由独立 worker 进程（python -m apps.worker）执行。
    knowledge / links / decisions：知识存储；缺省为 data_root 下的 JSON 存储（三者要么都给、要么都不给）。
    data_root：JSON 存储所在目录，缺省取 store.root；需要 JSON 存储而登记处没有 root 时必须提供，否则 ValueError。
    queries：有效视图查询，缺省为 FullScanQueries(store, knowledge, links, decisions)。
    批次 15d：scan_roots 为可扫描的服务器目录 {名称: 路径}（缺省无）；scan_index 缺省为 data_root（或 store.root）下的
    JsonScanIndex，两者都没有时为内存索引；scan_interval_minutes 给出时 Web 进程定时对全部根入队扫描；
    staging_dir 为续传暂存目录，缺省为 data_root（或 store.root）下的 uploads/，两者都没有时用临时目录。
    批次 16a：gate 为 OpenGate（缺省，开放模式）或 AccountGate（账号模式，见 apps/web/auth.py）；
    secure_cookie 为真时会话 Cookie 带 Secure（经 HTTPS 访问时使用）。
    批次 17a：mail 为邮件部件（MailParts，见 apps/web/mail.py）；缺省不启用邮件，/api/mail/* 返回 404。
    批次 19：conversations 为小安的对话记录；缺省为 data_root（或 store.root）下的 JSON 存储，两者都没有时为内存。"""
    given = [part is not None for part in (knowledge, links, decisions)]
    if any(given) and not all(given):
        raise ValueError("knowledge、links、decisions 必须同时提供")
    if not all(given):
        if data_root is None:
            data_root = getattr(store, "root", None)
            if data_root is None:
                raise ValueError("使用数据库登记处时必须提供 data_root")
        data_root = Path(data_root)
        knowledge, links, decisions = KnowledgeStore(data_root), LinkStore(data_root), DecisionStore(data_root)
    redactor = Redactor(secrets)
    gate = gate if gate is not None else OpenGate(store.group_tag)
    decision_store, link_store = decisions, links
    reviewed = ReviewedKnowledge(knowledge, decision_store)
    queries = queries if queries is not None else FullScanQueries(store, knowledge, link_store, decision_store)
    dispatcher = EventDispatcher(store, poll_seconds=event_poll_seconds)
    tasks = tasks if tasks is not None else InMemoryTaskRepo(china_now)
    common = dict(tasks=tasks, run_workers=run_workers, worker_id=worker_id, poll_seconds=worker_poll_seconds)
    link_targets = LinkTargets(link_store)
    link_view_service = LinkViews(store, link_store, decision_store, queries, link_targets)
    link_jobs = LinkJobs(store, queries, link_store, LinkService(services.llm), redactor, retry_delay=retry_delay,
                         targets=link_targets, **common)
    extraction_jobs = ExtractionJobs(store, knowledge, ExtractionService(services.llm), redactor, retry_delay=retry_delay,
                                     on_extracted=lambda version: dispatcher.notify(), **common)
    tagger = PermissionTagger(gate.permissions) if gate.enabled else LegacyTagger(store.group_tag)
    jobs = JobManager(store, services, redactor, retry_delay=retry_delay,
                      on_published=lambda version: dispatcher.notify(), tagger=tagger, **common)
    acl_sync = AclSyncJobs(gate.permissions, services.indexer, redactor, **common) if gate.enabled else None
    if acl_sync is not None:
        gate.permissions.set_on_change(acl_sync.enqueue)

    base = Path(data_root) if data_root is not None else (Path(store.root) if getattr(store, "root", None) else None)
    if scan_index is None:
        scan_index = JsonScanIndex(base) if base is not None else InMemoryScanIndex()
    if conversations is None:              # 批次 19
        conversations = ConversationStore(base) if base is not None else InMemoryConversationRepo()
    lifecycle = LifecycleService(store, services.indexer, tagger, now_iso)       # 批次 20a
    # 批次 21b：有重建记录时，彻底删除也从重建中、保留待回退的检索数据集中移除。
    forget = ((lambda version_id: forget_everywhere(version_id, store, rebuilds, services.indexer_for))
              if rebuilds is not None and services.indexer_for is not None else (lambda version_id: None))
    purger = PurgeService(store, services.indexer, knowledge, link_store, decision_store, conversations, services.blobs,
                          queries.merge_map, now_iso, forget=forget)              # 批次 20b
    scan_jobs = ScanJobs(scan_roots or {}, scan_index, jobs, redactor, max_bytes=max_upload_bytes, lifecycle=lifecycle,
                         **common)
    mail_jobs = (MailJobs(mail.roots, mail.index, mail.catalog, redactor, **common) if mail is not None else None)
    names = NameDirectory(queries.entity_names)          # 批次 18a：实体词典（知识数据不变时复用）
    if staging_dir is None:
        staging_dir = base / "uploads" if base is not None else Path(tempfile.mkdtemp(prefix="haina-uploads-"))
    staging_root = Path(staging_dir)
    stagings: dict[str, UploadStaging] = {}

    def staging_for(access: Access) -> UploadStaging:
        """续传暂存按账号分开：开放模式直接用暂存目录，账号模式用其下的 users/<user_id>/（批次 16a）。"""
        key = access.user.user_id if gate.enabled else ""
        if key not in stagings:
            root = staging_root / "users" / key if key else staging_root
            stagings[key] = UploadStaging(root, max_bytes=max_upload_bytes, clock=china_now)
        return stagings[key]

    def extract_published(event: dict[str, Any]) -> None:
        try:
            extraction_jobs.enqueue(event["version_id"], key=f"extract:{event['version_id']}:evt{event['seq']}")
        except (KeyError, ValueError):
            # 版本已不存在或不再是已发布：跳过，不阻塞后续事件。
            pass

    if auto_extract:
        dispatcher.subscribe("extraction", {DOCUMENT_PUBLISHED}, extract_published)
    if auto_links:
        dispatcher.subscribe("links", {EXTRACTION_DONE}, lambda event: link_jobs.on_extracted(event["version_id"]))

    def decision_changed(action: str, decision: Any) -> None:
        try:
            store.append_event(DECISION_CHANGED, None, {"action": action, "decision_id": decision.decision_id,
                                                        "kind": decision.kind.value, "target": decision.target})
        except Exception:
            # 审核决定已保存；事件写入失败不影响本次操作结果。
            pass

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        link_jobs.start()
        extraction_jobs.start()
        jobs.start()
        scan_jobs.start()
        if scan_interval_minutes is not None:
            scan_jobs.start_schedule(scan_interval_minutes)
        if mail_jobs is not None:
            mail.index.ensure()
            mail_jobs.start()
            if mail.interval_minutes is not None:
                mail_jobs.start_schedule(mail.interval_minutes)
        if acl_sync is not None:
            acl_sync.start()
            gate.permissions.ensure_default()
            acl_sync.enqueue(gate.permissions.repo.revision())      # 启动时核对一次检索标签（含存量文档迁移）
        try:
            UploadStaging(staging_root, max_bytes=max_upload_bytes, clock=china_now).purge()
            users_dir = staging_root / "users"
            for child in sorted(users_dir.iterdir()) if users_dir.is_dir() else []:
                if child.is_dir():
                    UploadStaging(child, max_bytes=max_upload_bytes, clock=china_now).purge()
        except OSError:
            pass
        dispatcher.start()
        if hasattr(queries, "warm"):
            # 批次 15b：数据库规模较大时全图冷计算需十几秒，启动后在后台先算好（见 core/app/query_cache.py）。
            queries.warm()
        try:
            yield
        finally:
            dispatcher.close()
            if acl_sync is not None:
                acl_sync.close()
            scan_jobs.close()
            if mail_jobs is not None:
                mail_jobs.close()
            jobs.close()
            extraction_jobs.close()
            link_jobs.close()

    app = FastAPI(title="海纳知识库", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.jobs = jobs
    app.state.dispatcher = dispatcher
    app.state.tasks = tasks
    app.state.scan_jobs = scan_jobs
    app.state.mail_jobs = mail_jobs
    app.state.staging_for = staging_for
    app.state.gate = gate
    app.state.extraction_jobs = extraction_jobs
    app.state.decision_store = decision_store
    series_service = SeriesService(store, decision_store)          # 批次 23c：文档系列
    app.state.series = series_service
    app.state.link_jobs = link_jobs
    app.state.acl_sync = acl_sync

    def response(data: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(redactor.clean(data), status_code=status)

    def current(request: Request) -> Access:
        """当前请求的身份（由 authenticate 中间件放入 request.state）。"""
        return request.state.access

    register_account_routes(app, gate, store, response=response, current=current, acl_sync=acl_sync,
                            secure_cookie=secure_cookie,
                            mail_folders=(lambda: mail.catalog.folder_counts(depth=MAIL_FOLDER_DEPTH))
                            if mail is not None else None)
    register_mail_routes(app, mail_jobs, scope_of=gate.mail_scope, response=response, current=current,
                         llm=services.llm, record_unanswered=store.record_unanswered, redact=redactor.text)

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any) -> Any:
        """账号模式：/api/ 下除健康检查与登录外都要有效会话（否则 401）；非 GET/HEAD 请求必须带 X-Haina-Request: 1
        （否则 403），用于防跨站请求伪造；再按路径要求角色（action_for，不允许 → 403）。开放模式只做角色判断（本机为管理员）。"""
        path = request.url.path
        if not path.startswith("/api/") or path in PUBLIC_PATHS:
            return await call_next(request)
        if gate.enabled and request.method not in ("GET", "HEAD") and request.headers.get(CSRF_HEADER) != "1":
            return response({"error": "缺少请求来源标记"}, 403)
        if path == "/api/auth/login":
            return await call_next(request)
        access = gate.resolve(request.cookies.get(COOKIE))
        if access is None:
            return response({"error": "请先登录"}, 401)
        action = action_for(request.method, path)
        if action is not None and not access.can(action):
            return response({"error": "没有执行该操作的权限"}, 403)
        request.state.access = access
        return await call_next(request)

    @app.middleware("http")
    async def unexpected_error(request: Any, call_next: Any) -> Any:
        try:
            return await call_next(request)
        except Exception as exc:
            # 在异常交给服务器日志之前脱敏，避免第三方异常包含凭据。
            return response({"error": "处理请求失败：" + redactor.error(exc)}, 500)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Any, exc: RequestValidationError) -> JSONResponse:
        return response({"error": "请求参数无效，请检查文本、文件和数量范围"}, 400)

    @app.exception_handler(HTTPException)
    async def http_error(request: Any, exc: HTTPException) -> JSONResponse:
        message = {404: "未找到该资源", 405: "不支持此操作"}.get(exc.status_code, str(exc.detail))
        return response({"error": message}, exc.status_code)

    @app.exception_handler(TransientError)
    async def transient_error(request: Any, exc: TransientError) -> JSONResponse:
        return response({"error": "服务暂时不可用：" + redactor.error(exc)}, 503)

    @app.exception_handler(PermanentError)
    async def permanent_error(request: Any, exc: PermanentError) -> JSONResponse:
        return response({"error": "服务请求失败：" + redactor.error(exc)}, 502)

    @app.get("/api/health")
    def health() -> JSONResponse:
        """账号模式下不需要登录，因此只返回 {"ok": true, "auth": true, "model_location": …}；开放模式另含数据集名与文档数。
        model_location（批次 21a）："online" 时界面提示“当前使用在线服务，仅限样例数据”。"""
        if gate.enabled:
            return response({"ok": True, "auth": True, "model_location": model_location})
        return response({"ok": True, "dataset_name": store.dataset_name,
                         "documents": len(store.documents()), "model_location": model_location})

    @app.get("/api/overview")
    def overview(access: Access = Depends(current)) -> JSONResponse:
        """批次 16b：统计与最近 10 份由登记处按可读范围计算，不再取出全部记录。"""
        scope = gate.scope(access)
        return response({**store.document_stats(scope), "unanswered": len(store.unanswered()),
                         "recent": store.document_page(scope, limit=10)["rows"]})

    @app.get("/api/documents")
    def documents(status: str = "", q: str = "", offset: int = 0, limit: int | None = None,
                  access: Access = Depends(current)) -> JSONResponse:
        """不给 limit：与此前相同，返回全部符合条件的记录列表（新的在前）。
        给出 limit（1–500）：分页，返回 {"rows","total","offset","limit"}（批次 16b，文档库界面使用）。参数超出范围 → 400。"""
        scope = gate.scope(access)
        try:
            if limit is None:
                total = store.document_stats(scope)["documents"]
                rows: list[dict[str, Any]] = []
                while len(rows) < total:
                    page = store.document_page(scope, status=status or None, query=q or None, offset=len(rows),
                                               limit=PAGE_LIMIT_MAX)
                    rows.extend(page["rows"])
                    if not page["rows"] or len(rows) >= page["total"]:
                        break
                return response(with_series(rows))
            page = store.document_page(scope, status=status or None, query=q or None, offset=offset, limit=limit)
        except ValueError:
            return response({"error": "分页参数超出范围"}, 400)
        return response({**page, "rows": with_series(page["rows"]), "offset": offset, "limit": limit})

    def with_series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """批次 23c：每行加 "series"：{"id","name"}（文档是某系列的成员时）或 None。"""
        labels = series_service.labels(row["version_id"] for row in rows)
        return [{**row, "series": labels.get(row["version_id"])} for row in rows]

    def readable_detail(access: Access, version_id: str) -> dict[str, Any] | None:
        """可读的文档详情；不存在或无权读取都返回 None（接口一律 404，不暴露文档是否存在）。"""
        detail = store.detail(version_id)
        if detail is None or not gate.can_read(access, detail["document"]):
            return None
        return detail

    def duplicate_body(access: Access, version_id: str, job_id: str | None) -> dict[str, Any]:
        """重复上传的响应：已有版本可读时才带上版本号与作业 ID（批次 16a）。"""
        body: dict[str, Any] = {"error": "该文件已导入"}
        if readable_detail(access, version_id) is not None or (job_id and job_owned(access, job_id)):
            body["version_id"] = version_id
            if job_id is not None:
                body["job_id"] = job_id
        return body

    def readable_check(access: Access) -> Callable[[str], bool]:
        """一次请求用的“版本是否可读”判断（批次 16b：按可读范围与 folders_of 判断，不取全部记录）。"""
        scope = gate.scope(access)
        if scope is None:
            return lambda version_id: True

        def check(version_id: str) -> bool:
            folder = store.folders_of([version_id]).get(version_id)
            return folder is not None and scope.contains(version_id, folder)
        return check

    def searchable_check(access: Access) -> Callable[[str], bool]:
        """检索命中的过滤（批次 20a）：版本可读，且当前状态为已发布。撤下时检索引擎的删除失败或尚未生效，
        已撤下、已替代文档的命中也不会进入检索结果与问答资料。"""
        readable = readable_check(access)

        def check(version_id: str) -> bool:
            record = store.records([version_id]).get(version_id)
            return record is not None and record["status"] == "published" and readable(version_id)
        return check

    def job_owned(access: Access, job_id: str) -> bool:
        owner = jobs.owner(job_id)
        return owner is not None and (not gate.enabled or owner[0] == access.user.user_id)

    @app.get("/api/documents/{version_id}")
    def document(version_id: str, access: Access = Depends(current)) -> JSONResponse:
        """批次 23c：另加 "series"：SeriesService.of_document（按当前账号的可读范围）或 None。"""
        detail = readable_detail(access, version_id)
        if detail is None:
            raise HTTPException(404)
        return response({**detail, "series": series_service.of_document(version_id, gate.scope(access))})

    @app.get("/api/series")
    def series_list(q: str = "", access: Access = Depends(current)) -> JSONResponse:
        """批次 23c：{"series": SeriesService.list(范围, query=q)}。"""
        return response({"series": series_service.list(gate.scope(access), query=q)})

    @app.get("/api/series/{series_id}")
    def series_detail(series_id: str, access: Access = Depends(current)) -> JSONResponse:
        """批次 23c：SeriesService.detail；不存在或没有可读成员 → 404。"""
        data = series_service.detail(series_id, gate.scope(access))
        if data is None:
            raise HTTPException(404)
        return response(data)

    def series_scope(access: Access, series: str) -> Visibility | None:
        """批次 23c：时间轴、图谱的 series 参数。为空 → 账号原有范围；否则限定到该系列的可读成员，
        系列不存在或没有可读成员 → 404。"""
        scope = gate.scope(access)
        if not series:
            return scope
        limited = series_service.scope_for(series, scope)
        if limited is None:
            raise HTTPException(404)
        return limited

    @app.get("/api/documents/{version_id}/links")
    def document_links(version_id: str, access: Access = Depends(current)) -> JSONResponse:
        """批次 18c：一份文档的关系。{"links": 来源或目标是该版本的已对齐引用（含替代）, "conflicts": 涉及该版本的冲突}，
        取自当前账号范围内的视图，顺序同 /api/links。文档不存在或不可读 → 404。"""
        if readable_detail(access, version_id) is None:
            raise HTTPException(404)
        data = link_views(gate.scope(access))
        return response({"links": [row for row in data["links"] if version_id in (row["source"], row["target"])],
                         "conflicts": [row for row in data["conflicts"]
                                       if version_id in (row["version_a"], row["version_b"])]})

    @app.post("/api/documents")
    async def upload(file: UploadFile = File(...), access: Access = Depends(current)) -> JSONResponse:
        try:
            if not gate.can_upload(access, ""):
                return response({"error": "没有向该文件夹上传的权限"}, 403)
            filename = PureWindowsPath(file.filename or "").name
            problem = upload_name_error(filename)
            if problem is not None:
                return response({"error": problem}, 400)
            parts, size = [], 0
            while part := await file.read(1024 * 1024):
                size += len(part)
                if size > max_upload_bytes:
                    return response({"error": "文件大小超过 200 MB 上限"}, 413)
                parts.append(part)
            if size == 0:
                return response({"error": "不能导入空文件"}, 400)
            try:
                job_id = jobs.submit(filename, b"".join(parts), by=access.user.user_id)
            except DuplicateUpload as exc:
                return response(duplicate_body(access, exc.version_id, exc.job_id), 409)
            return response({"job_id": job_id}, 202)
        finally:
            await file.close()

    @app.post("/api/documents/{version_id}/retry")
    def retry_document(version_id: str, access: Access = Depends(current)) -> JSONResponse:
        """重新导入失败的文档（批次 15c）：返回 {"job_id"}，202。版本不存在或不可读 → 404；不可重试 → 400。"""
        if readable_detail(access, version_id) is None:
            raise HTTPException(404)
        try:
            job_id = jobs.retry(version_id, by=access.user.user_id)
        except KeyError:
            raise HTTPException(404)
        except ValueError as exc:
            return response({"error": str(exc)}, 400)
        return response({"job_id": job_id}, 202)

    def lifecycle_call(call: Callable[[], Any]) -> Any:
        """撤下、恢复、确认版本的错误映射：KeyError → 404；ValueError → 400；检索引擎错误 → 502（登记处不变）。"""
        try:
            return call()
        except KeyError:
            raise HTTPException(404)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        except (TransientError, PermanentError) as exc:
            raise HTTPException(502, detail=redactor.error(exc))

    def lifecycle_actor(access: Access) -> str:
        return access.user.user_id if gate.enabled else "local"

    @app.post("/api/documents/{version_id}/withdraw")
    def withdraw_document(version_id: str, body: _WithdrawRequest, access: Access = Depends(current)) -> JSONResponse:
        """撤下（批次 20a）：已发布 → 已撤下，从检索中移除，记录与分块保留。{"reason"}（可选，最多 200 字）。
        不可读或不存在 → 404；状态不允许 → 400；检索引擎失败 → 502。返回 {"document": 更新后的记录}。"""
        if readable_detail(access, version_id) is None:
            raise HTTPException(404)
        return response({"document": lifecycle_call(lambda: lifecycle.withdraw(
            version_id, by=lifecycle_actor(access), reason=redactor.text(body.reason)))})

    @app.post("/api/documents/{version_id}/restore")
    def restore_document(version_id: str, access: Access = Depends(current)) -> JSONResponse:
        """恢复（批次 20a）：已撤下或已替代 → 已发布（用保存的分块重新写入检索引擎，不重新转换、不重新抽取）。
        错误同 withdraw。返回 {"document": 更新后的记录}。"""
        if readable_detail(access, version_id) is None:
            raise HTTPException(404)
        return response({"document": lifecycle_call(lambda: lifecycle.restore(version_id, by=lifecycle_actor(access)))})

    @app.post("/api/documents/{version_id}/keep")
    def keep_document(version_id: str, access: Access = Depends(current)) -> JSONResponse:
        """确认保留该版本（批次 20a）：同一文档中当前账号可读的其他已发布版本变为已替代。
        返回 {"kept": version_id, "superseded": [版本号…]}。错误同 withdraw。"""
        if readable_detail(access, version_id) is None:
            raise HTTPException(404)
        readable = readable_check(access)
        superseded = lifecycle_call(lambda: lifecycle.keep(version_id, by=lifecycle_actor(access), allowed=readable))
        return response({"kept": version_id, "superseded": superseded})

    @app.post("/api/documents/{version_id}/purge")
    def purge_document(version_id: str, body: _PurgeRequest, access: Access = Depends(current)) -> JSONResponse:
        """彻底删除（批次 20b）：只有管理员（PURGE）；文档须可读，否则 404；confirm 必须等于版本号，否则 400；
        只能删除已撤下或已替代的文档，否则 400；检索引擎等外部错误 → 502（文档仍为已撤下，可以重做）。
        不可恢复。返回 {"report": PurgeService 的删除报告, "document": 墓碑记录}。"""
        if readable_detail(access, version_id) is None:
            raise HTTPException(404)
        if body.confirm != version_id:
            return response({"error": "请输入版本号以确认彻底删除"}, 400)
        report = lifecycle_call(lambda: purger.purge(version_id, by=lifecycle_actor(access)))
        return response({"report": report, "document": store.detail(version_id)["document"]})

    @app.get("/api/documents/{version_id}/versions")
    def document_versions(version_id: str, access: Access = Depends(current)) -> JSONResponse:
        """同一文档（同文件夹、同名）的全部版本（批次 20a）：{"versions": [当前账号可读的记录…按保存先后]}。
        不可读或不存在 → 404。"""
        detail = readable_detail(access, version_id)
        if detail is None:
            raise HTTPException(404)
        scope = gate.scope(access)
        return response({"versions": [row for row in same_document(store.documents(), detail["document"])
                                      if scope is None or scope.contains(row["version_id"], row.get("folder", ""))]})

    @app.post("/api/documents/{version_id}/review")
    def review_document(version_id: str, body: _DocumentReviewRequest, access: Access = Depends(current)) -> JSONResponse:
        """文档审核（批次 15c）：action 为 "dismiss"（确认无需处理，移出审核列表，note 可选）或 "undo"（撤销确认）。
        版本不存在或不可读 → 404；action 无效、对已重试的文档撤销/确认、对不需要审核的文档确认 → 400。返回 {"document": 更新后的记录}。"""
        detail = readable_detail(access, version_id)
        if detail is None:
            raise HTTPException(404)
        record = detail["document"]
        current = (record.get("review") or {}).get("action")
        if body.action == "dismiss":
            if current is not None or not needs_review({**record, "review": None}):
                return response({"error": "该文档不需要确认或已处理"}, 400)
            review = {"action": "dismissed", "at": now_iso()}
            if body.note:
                review["note"] = redactor.text(body.note)
            return response({"document": store.set_review(version_id, review)})
        if body.action == "undo":
            if current != "dismissed":
                return response({"error": "只能撤销“已确认”的文档"}, 400)
            return response({"document": store.set_review(version_id, None)})
        return response({"error": "不支持的文档审核操作"}, 400)

    # —— 批次 15d：文件夹上传（可续传）——

    def _import_target(path: str, size: Any, sha256: Any) -> tuple[str, str] | str:
        """校验一项文件夹上传，返回 (文件夹, 文件名) 或中文错误原因。"""
        try:
            folder, filename = split_import_path(path)
        except ValueError:
            return "文件路径无效"
        problem = upload_name_error(filename)
        if problem is not None:
            return problem
        if not isinstance(size, int) or isinstance(size, bool) or size < 1:
            return "不能导入空文件"
        if size > max_upload_bytes:
            return "文件大小超过 200 MB 上限"
        if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None:
            return "文件哈希无效"
        return folder, filename

    @app.get("/api/uploads/config")
    def upload_config() -> JSONResponse:
        """前端文件夹上传用：{"extensions": 支持的扩展名（升序）, "max_bytes", "chunk_bytes"（建议的分块大小）, "check_batch"}。"""
        return response({"extensions": sorted(SUPPORTED_EXTENSIONS), "max_bytes": max_upload_bytes,
                         "chunk_bytes": 4 * 1024 * 1024, "check_batch": 1000})

    @app.post("/api/uploads/check")
    def check_uploads(body: _UploadCheckRequest, access: Access = Depends(current)) -> JSONResponse:
        """逐项返回 {"path", "state": "exists"|"importing"|"partial"|"new"|"rejected", 以及
        "version_id"（exists、importing）、"job_id"（importing）、"received"（partial）、"error"（rejected）}，顺序同请求。
        批次 16a：没有向目标文件夹上传的权限 → rejected；已有版本不可读时 exists / importing 不带版本号与作业 ID。"""
        staging = staging_for(access)
        results = []
        for item in body.files:
            row: dict[str, Any] = {"path": item.path}
            target = _import_target(item.path, item.size, item.sha256)
            if not isinstance(target, str) and not gate.can_upload(access, target[0]):
                target = "没有向该文件夹上传的权限"
            if isinstance(target, str):
                row.update(state="rejected", error=target)
            elif (found := jobs.lookup(item.sha256)) is not None:
                visible = duplicate_body(access, found["version_id"], found.get("job_id"))
                row.update(state=found["state"], **{k: v for k, v in visible.items() if k != "error"})
            elif (partial := staging.status(item.sha256)) is not None and partial["size"] == item.size:
                row.update(state="partial", received=partial["received"])
            else:
                row.update(state="new")
            results.append(row)
        return response({"results": results})

    @app.post("/api/uploads")
    def begin_upload(body: _UploadItem, access: Access = Depends(current)) -> JSONResponse:
        """开始或续传：{"upload_id", "received", "size"}。已导入 → 409 {"error","version_id"}；导入中 → 409 再加 "job_id"；
        不合规 → 400；没有向目标文件夹上传的权限 → 403。"""
        target = _import_target(body.path, body.size, body.sha256)
        if isinstance(target, str):
            return response({"error": target}, 400)
        if not gate.can_upload(access, target[0]):
            return response({"error": "没有向该文件夹上传的权限"}, 403)
        found = jobs.lookup(body.sha256)
        if found is not None:
            return response(duplicate_body(access, found["version_id"], found.get("job_id")), 409)
        return response(staging_for(access).begin(body.sha256, body.size, body.path))

    @app.get("/api/uploads/{upload_id}")
    def upload_status(upload_id: str, access: Access = Depends(current)) -> JSONResponse:
        try:
            value = staging_for(access).status(upload_id)
        except ValueError:
            raise HTTPException(404)
        if value is None:
            raise HTTPException(404)
        return response({key: value[key] for key in ("upload_id", "received", "size")})

    @app.put("/api/uploads/{upload_id}")
    async def upload_chunk(upload_id: str, request: Request, offset: int = -1,
                           access: Access = Depends(current)) -> JSONResponse:
        """请求体为原始字节，追加在 offset 处：{"received"}。不存在 → 404；offset 不符 → 409 {"error","received"}；
        分块为空、超过 8 MB 或超出文件大小 → 400。"""
        data = await request.body()
        try:
            received = staging_for(access).append(upload_id, offset, data)
        except KeyError:
            raise HTTPException(404)
        except OffsetMismatch as exc:
            return response({"error": "上传位置不一致", "received": exc.received}, 409)
        except ValueError as exc:
            if "十六进制" in str(exc):
                raise HTTPException(404)
            return response({"error": str(exc)}, 400)
        return response({"received": received})

    @app.post("/api/uploads/{upload_id}/complete")
    def complete_upload(upload_id: str, access: Access = Depends(current)) -> JSONResponse:
        """收齐后校验哈希并提交导入（文件夹取 begin 时的 path）：202 {"job_id"}。不存在 → 404；未收齐、哈希不符 → 400；
        期间已有同内容导入 → 409；没有向目标文件夹上传的权限 → 403（保留暂存）。"""
        staging = staging_for(access)
        try:
            current = staging.status(upload_id)
        except ValueError:
            raise HTTPException(404)
        if current is None:
            raise HTTPException(404)
        target = _import_target(current["path"], current["size"], upload_id)
        if isinstance(target, str):
            staging.discard(upload_id)
            return response({"error": target}, 400)
        if not gate.can_upload(access, target[0]):
            return response({"error": "没有向该文件夹上传的权限"}, 403)
        try:
            data = staging.finish(upload_id)
        except ValueError as exc:
            return response({"error": str(exc)}, 400)
        folder, filename = target
        try:
            job_id = jobs.submit(filename, data, folder=folder, by=access.user.user_id)
        except DuplicateUpload as exc:
            return response(duplicate_body(access, exc.version_id, exc.job_id), 409)
        except ValueError as exc:
            return response({"error": str(exc)}, 400)
        return response({"job_id": job_id}, 202)

    @app.delete("/api/uploads/{upload_id}")
    def cancel_upload(upload_id: str, access: Access = Depends(current)) -> JSONResponse:
        try:
            staging_for(access).discard(upload_id)
        except ValueError:
            raise HTTPException(404)
        return response({"ok": True})

    # —— 批次 15d：服务器目录扫描 ——

    @app.get("/api/scan")
    def scan_status() -> JSONResponse:
        """{"roots": [{"name","path","active","last_run"}…], "interval_minutes"}，见 ScanJobs.status。"""
        return response(scan_jobs.status())

    @app.post("/api/scan")
    def start_scan(body: _ScanRequest) -> JSONResponse:
        """{"root"} → 202 {"task_id"}；根不存在 → 404；正在扫描 → 409 {"error","task_id"}。"""
        try:
            task_id = scan_jobs.enqueue(body.root)
        except KeyError:
            raise HTTPException(404)
        except ScanBusy as exc:
            return response({"error": "该目录正在扫描", "task_id": exc.task_id}, 409)
        return response({"task_id": task_id}, 202)

    @app.get("/api/scan/runs")
    def scan_runs(root: str, limit: int = 20) -> JSONResponse:
        """某个根最近的扫描报告（不含 items），新的在前；limit 取 1–100。根不存在 → 404。"""
        if not 1 <= limit <= 100:
            return response({"error": "limit 取 1–100"}, 400)
        try:
            return response({"runs": scan_jobs.runs(root, limit)})
        except KeyError:
            raise HTTPException(404)

    @app.get("/api/scan/runs/{scan_id}")
    def scan_run(scan_id: str) -> JSONResponse:
        """完整报告（含 items）；不存在或不属于已配置的根 → 404。"""
        report = scan_jobs.run(scan_id)
        if report is None or report["root"] not in scan_jobs.roots:
            raise HTTPException(404)
        return response(report)

    @app.get("/api/tasks")
    def task_status() -> JSONResponse:
        """任务队列概况（批次 15c）：{种类: {"queued","running","done","failed"}}，种类为 ingest、extract、links、scan（15d）、
        mail_scan（17a，只在启用邮件时列出）。"""
        kinds = ("ingest", "extract", "links", "scan") + (("mail_scan",) if mail_jobs is not None else ())
        return response({kind: {status.value: tasks.count(kind, status) for status in TaskStatus} for kind in kinds})

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str, access: Access = Depends(current)) -> JSONResponse:
        """账号模式：提交者本人，或能读取该作业所导入文档的账号才能查看，否则 404（批次 16a）。"""
        value = jobs.get(job_id)
        if value is None:
            raise HTTPException(404)
        if gate.enabled and not job_owned(access, job_id) and readable_detail(access, value["version_id"]) is None:
            raise HTTPException(404)
        return response(value)

    @app.post("/api/search")
    def search(body: _SearchRequest, access: Access = Depends(current)) -> JSONResponse:
        """批次 16a：按当前账号的用户组检索，结果再按最新授权过滤一遍（标签同步完成前也不会越权）。"""
        catalog, titles, filenames = store.context()
        indexer = FilteringIndexer(services.indexer, searchable_check(access))
        hits = resolve_hits(indexer.search(body.query, group_tags=gate.search_tags(access), top_k=body.top_k), catalog)
        return response({"hits": [{"rank": i, "version_id": hit.version_id, "chunk_id": hit.chunk_id,
                                    "title": titles.get(hit.version_id, hit.version_id), "filename": filenames.get(hit.version_id),
                                    "section": hit.section_path, "page_start": hit.page_start, "page_end": hit.page_end,
                                    "score": hit.score, "text": strip_breadcrumb(hit.text)} for i, hit in enumerate(hits, 1)]})

    @app.post("/api/ask")
    def ask(body: _AskRequest, access: Access = Depends(current)) -> JSONResponse:
        started = time.perf_counter()
        catalog, titles, filenames = store.context()
        indexer = FilteringIndexer(services.indexer, searchable_check(access))
        result = QaService(indexer, services.llm, catalog, titles).ask(body.question, group_tags=gate.search_tags(access))
        data = asdict(result)
        for field in ("citations", "sources"):
            data[field] = [{**item, "filename": filenames.get(item["version_id"])} for item in data[field]]
        data["seconds"] = round(time.perf_counter() - started, 1)
        if result.status in ("no_hits", "insufficient"):
            store.record_unanswered(redactor.text(body.question), result.status)
        return response(data)

    def conversation_owner(access: Access) -> str:
        """对话的所有者：账号模式为用户 ID；开放模式为 "local"（批次 19）。"""
        return access.user.user_id if gate.enabled else "local"

    def owned_conversation(access: Access, conversation_id: str) -> Conversation:
        """本人的对话；ID 格式无效、不存在或不属于本人 → 404（不暴露对话是否存在）。"""
        conversation = conversations.get(conversation_id) if valid_conversation_id(conversation_id) else None
        if conversation is None or conversation.summary.owner != conversation_owner(access):
            raise HTTPException(404)
        return conversation

    def source_readable(access: Access) -> Callable[[Mapping[str, Any]], bool]:
        """一条资料（CrossSource 的字典）当前是否可读：文档与事件按版本可读范围，邮件按文件夹可读范围。"""
        readable, mail_scope = readable_check(access), gate.mail_scope(access)

        def check(source: Mapping[str, Any]) -> bool:
            if source.get("kind") == "mail":
                return mail_scope is None or mail_scope.contains(source.get("folder") or "")
            return bool(source.get("version_id")) and readable(source["version_id"])
        return check

    def summary_json(summary: ConversationSummary) -> dict[str, Any]:
        return {key: value for key, value in asdict(summary).items() if key != "owner"}

    @app.post("/api/assistant/ask")
    def assistant_ask(body: _AssistantRequest, access: Access = Depends(current)) -> JSONResponse:
        """跨来源问答（批次 18a）：{"question", "mode": "all"（缺省）| "documents" | "mails", "conversation_id"（批次 19，可选）}
        → CrossQaResult 的字段（asdict）加 "seconds"、"conversation_id"、"turn"（本轮序号，从 0 起）。
        - 问题为空、mode 无效 → 400；未启用邮件时 mode 为 "mails" → 400，"all" 只查文档与知识事件。
        - 批次 19：conversation_id 给出时继续该对话（不是本人的或不存在 → 404，已有 MAX_TURNS 轮 → 400），
          上文为该对话中当前仍可读的最近几轮（history_of(visible_turn(...))）；缺省时新建对话（标题为 title_of(问题)）。
          作答成功后才创建对话、追加本轮，模型调用失败不留下空对话。
        - 没有命中或资料不足时记录未收录问题（问题为改写后的完整问题，状态为 "assistant_no_hits" / "assistant_insufficient"）。"""
        started = time.perf_counter()
        if body.mode == "mails" and mail_jobs is None:
            return response({"error": "未启用邮件"}, 400)
        history: list[HistoryTurn] = []
        if body.conversation_id is not None:
            conversation = owned_conversation(access, body.conversation_id)
            if conversation.summary.turns >= MAX_TURNS:
                return response({"error": f"一个对话最多 {MAX_TURNS} 轮，请新建对话"}, 400)
            readable = source_readable(access)
            history = history_of([visible_turn(turn, readable) for turn in conversation.turns])
        documents = RegistryDocuments(FilteringIndexer(services.indexer, searchable_check(access)),
                                      gate.search_tags(access), store)
        service = CrossQaService(services.llm, documents=documents,
                                 mails=mail_jobs.index if mail_jobs is not None else None, knowledge=queries, names=names)
        try:
            result = service.ask(body.question, mode=body.mode, visibility=gate.scope(access),
                                 mail_scope=gate.mail_scope(access), history=history)
        except ValueError as exc:
            return response({"error": str(exc)}, 400)
        if result.status in ("no_hits", "insufficient"):
            store.record_unanswered(redactor.text(result.standalone), f"assistant_{result.status}")
        at, data = now_iso(), asdict(result)
        conversation_id = body.conversation_id
        if conversation_id is None:
            conversation_id = new_conversation_id()
            conversations.create(conversation_id, conversation_owner(access), title_of(result.question), at)
        summary = conversations.append(conversation_id, turn_record(data, at=at), at)
        return response({**data, "seconds": round(time.perf_counter() - started, 1), "conversation_id": conversation_id,
                         "turn": summary.turns - 1})

    @app.get("/api/assistant/conversations")
    def conversation_list(offset: int = 0, limit: int = 50, access: Access = Depends(current)) -> JSONResponse:
        """本人的对话（批次 19）：{"conversations": [摘要…按最近更新], "total"}。limit 取 1–200，offset ≥ 0，否则 400。
        摘要为 conversation_id、title、created_at、updated_at、turns。"""
        if offset < 0 or not 1 <= limit <= 200:
            return response({"error": "分页参数无效"}, 400)
        owner = conversation_owner(access)
        return response({"conversations": [summary_json(row) for row in conversations.summaries(owner, limit=limit,
                                                                                                 offset=offset)],
                         "total": conversations.count(owner)})

    @app.get("/api/assistant/conversations/{conversation_id}")
    def conversation_detail(conversation_id: str, access: Access = Depends(current)) -> JSONResponse:
        """本人的一个对话（批次 19）：{**摘要, "turns": [visible_turn(...)…]}：引用的资料中有当前不可读内容的轮次，
        回答与资料隐藏（hidden 为真）。不是本人的或不存在 → 404。"""
        conversation = owned_conversation(access, conversation_id)
        readable = source_readable(access)
        return response({**summary_json(conversation.summary),
                         "turns": [visible_turn(turn, readable) for turn in conversation.turns]})

    @app.patch("/api/assistant/conversations/{conversation_id}")
    def conversation_rename(conversation_id: str, body: _RenameRequest, access: Access = Depends(current)) -> JSONResponse:
        """改标题（批次 19）：title 去首尾空白后 1–MAX_TITLE_CHARS 字，否则 400。返回摘要。"""
        owned_conversation(access, conversation_id)
        title = " ".join(body.title.split())
        if not 1 <= len(title) <= MAX_TITLE_CHARS:
            return response({"error": f"标题须为 1–{MAX_TITLE_CHARS} 字"}, 400)
        return response(summary_json(conversations.rename(conversation_id, title, now_iso())))

    @app.delete("/api/assistant/conversations/{conversation_id}")
    def conversation_delete(conversation_id: str, access: Access = Depends(current)) -> JSONResponse:
        """删除本人的对话（批次 19），永久删除、不可恢复：{"ok": true}。不是本人的或不存在 → 404。"""
        owned_conversation(access, conversation_id)
        conversations.delete(conversation_id)
        return response({"ok": True})

    @app.get("/api/review")
    def review(access: Access = Depends(current)) -> JSONResponse:
        """批次 16b：文档、实体、事件、引用、冲突都只含当前账号可读文档中的部分。
        批次 18c：待确认引用（links）只给前 REVIEW_LINKS 条（按 id 升序），links_total 为总数。
        批次 20a：versions 为版本待确认的组（version_groups，只用可读文档），最多 REVIEW_VERSIONS 组，versions_total 为总组数。"""
        scope = gate.scope(access)
        selected = [row for row in store.documents()
                    if needs_review(row) and (scope is None or scope.contains(row["version_id"], row.get("folder", "")))]
        view = link_views(scope)
        groups = version_groups(row for row in store.documents()
                                if scope is None or scope.contains(row["version_id"], row.get("folder", "")))
        return response({"documents": _recent(selected),
                         "unanswered": pending_questions(reversed(store.unanswered()), decision_store.decisions())[:200],
                         "entities": queries.review_entities(scope=scope), "events": queries.review_events(scope=scope),
                         "links": view["pending"][:REVIEW_LINKS], "links_total": len(view["pending"]),
                         "versions": groups[:REVIEW_VERSIONS], "versions_total": len(groups),
                         "conflicts": [row for row in view["conflicts"] if not row["decided"]]})

    def link_views(scope: Visibility | None = None) -> dict[str, Any]:
        """引用与冲突视图（批次 18c 起由 LinkViews 计算并缓存；返回值由多个请求共用，不得修改）。"""
        return link_view_service.view(scope)

    def decision_visible(access: Access, kind: DecisionKind, target: str, value: str | None) -> bool:
        """批次 16b-Q2：审核对象是否都在当前账号的可读范围内（开放模式恒为真）。
        实体：在范围内的实体详情存在（被可读文档引用）；合并时 value 指向的实体也要可见。
        事件、引用：ID 前缀的版本可读；引用指向的版本（value 非空时）也要可读。冲突：一对事件所在的两个版本都可读。
        未收录问题：不与文档关联，按角色控制。
        批次 23b：修改子类按实体判断；否决关系按关系 ID 前缀的版本判断。
        批次 23c：系列决定交给 SeriesService.decision_visible。"""
        scope = gate.scope(access)
        if kind in SERIES_KINDS:
            return series_service.decision_visible(kind, target, value, scope)
        if scope is None:
            return True
        check = readable_check(access)

        def entity(id_: str | None) -> bool:
            return bool(id_) and queries.entity_detail(id_, limit=1, scope=scope) is not None

        if kind is DecisionKind.ENTITY_MERGE:
            return entity(target) and (value is None or entity(value))
        if kind in (DecisionKind.ENTITY_DISTINCT, DecisionKind.ENTITY_SUBTYPE):     # 批次 23b：修改子类同实体
            return entity(target)
        if kind in (DecisionKind.EVENT_PASS, DecisionKind.EVENT_REJECT, DecisionKind.RELATION_REJECT):
            return check(target.split(":", 1)[0])
        if kind is DecisionKind.REFERENCE_SET:
            return check(target.split(":", 1)[0]) and (not value or check(value))
        if kind in (DecisionKind.CONFLICT_CONFIRM, DecisionKind.CONFLICT_DISMISS):
            parts = target.split("|")
            return len(parts) == 2 and all(check(part.split(":", 1)[0]) for part in parts)
        return True

    @app.post("/api/review/decisions")
    def create_decision(body: _DecisionRequest, access: Access = Depends(current)) -> JSONResponse:
        """审核对象不在可读范围内 → 404（与对象不存在相同，不暴露存在性）。"""
        try:
            kind = DecisionKind(body.kind)
        except ValueError:
            return response({"error": "不支持的审核操作"}, 400)
        if not decision_visible(access, kind, body.target, body.value):
            raise HTTPException(404)
        try:
            decision = decision_store.create(
                (lambda number: series_service.decide(kind, body.target, body.value, number=number,
                                                      created_at=now_iso())) if kind in SERIES_KINDS else
                (lambda number: make_decision(kind, body.target, body.value, number=number, created_at=now_iso(),
                                              context=queries.review_context(kind, body.target, body.value))))
        except KeyError:
            raise HTTPException(404)
        except ReviewError as exc:
            return response({"error": str(exc)}, 400)
        decision_changed("created", decision)
        return response({"decision": decision.to_json()}, 201)

    @app.delete("/api/review/decisions/{decision_id}")
    def remove_decision(decision_id: str, access: Access = Depends(current)) -> JSONResponse:
        """撤销决定：决定不存在或其对象不在可读范围内 → 404。"""
        current_decision = next((row for row in decision_store.decisions() if row.decision_id == decision_id), None)
        if current_decision is None or not decision_visible(access, current_decision.kind, current_decision.target,
                                                            current_decision.value):
            raise HTTPException(404)
        decision = decision_store.remove(decision_id)
        if decision is None:
            raise HTTPException(404)
        decision_changed("removed", decision)
        return response({"decision": decision.to_json()})

    @app.get("/api/events")
    def knowledge_events(after: int = 0, limit: int = 100) -> JSONResponse:
        """知识事件与订阅进度，供运维排查。limit 取 1–500，超出范围 → 400。"""
        if not 1 <= limit <= 500 or after < 0:
            return response({"error": "参数超出范围"}, 400)
        return response({"events": store.events(after, limit), "subscribers": dispatcher.status()})

    @app.get("/api/review/decisions")
    def decisions(access: Access = Depends(current)) -> JSONResponse:
        """最近 200 条决定（新的在前），只含对象在可读范围内的（批次 16b-Q2）。"""
        rows = [row for row in reversed(decision_store.decisions())
                if decision_visible(access, row.kind, row.target, row.value)]
        return response({"decisions": [row.to_json() for row in rows[:200]]})

    def scoped_jobs(access: Access, status: dict[str, Any], field: str,
                    version_of: Callable[[str], str | None]) -> dict[str, Any]:
        """作业状态按可读范围裁剪（批次 16b-Q2）：queued、running、failed 只留条目版本可读的（与具体版本无关的条目保留），
        idle 按裁剪后的结果计算。开放模式原样返回。"""
        if gate.scope(access) is None:
            return status
        check = readable_check(access)

        def ok(key: str | None) -> bool:
            version = version_of(key) if key is not None else None
            return version is None or check(version)

        queued = [key for key in status["queued"] if ok(key)]
        running = status["running"] if status["running"] is not None and ok(status["running"]) else None
        failed = [row for row in status["failed"] if ok(row.get(field))]
        return {**status, "queued": queued, "running": running, "failed": failed,
                "idle": not queued and running is None}

    @app.get("/api/links")
    def links_status(category: str = "", offset: int = 0, limit: int = LINK_PAGE, access: Access = Depends(current)) -> JSONResponse:
        """批次 18c：links / pending / unresolved 分页。category 为空时三类各给 [0, limit)，offset 必须为 0；
        category 为其中一类时只给该类的 [offset, offset + limit)，另两类为空列表。limit 取 1–LINK_PAGE_MAX。
        参数无效 → 400。status 中的计数始终是全量；另有 cites、supersedes（links 中两种关系的条数）与 page。
        conflicts 不分页（只含已判断的事件对，数量受冲突判断作业限制）。"""
        if category not in ("", "links", "pending", "unresolved") or offset < 0 or not 1 <= limit <= LINK_PAGE_MAX \
                or (not category and offset):
            return response({"error": "分页参数无效"}, 400)
        scope = gate.scope(access)
        job_status = scoped_jobs(access, link_jobs.status(), "key",
                                 lambda key: key.removeprefix("refs:") if key.startswith("refs:") else None)
        data = link_views(scope)
        published = {row["version_id"] for row in store.documents() if row["status"] == "published"
                     and (scope is None or scope.contains(row["version_id"], row.get("folder", "")))}
        rows = {name: (data[name][offset:offset + limit] if category in ("", name) else [])
                for name in ("links", "pending", "unresolved")}
        return response({**rows, "conflicts": data["conflicts"], "page": {"category": category or None, "offset": offset,
                                                                          "limit": limit}, "status": {
            "published": len(published), "extracted": len(link_store.extracted() & published),
            "links": len(data["links"]), "pending": len(data["pending"]), "unresolved": len(data["unresolved"]),
            "cites": sum(row["kind"] == "cites" for row in data["links"]),
            "supersedes": sum(row["kind"] == "supersedes" for row in data["links"]),
            "conflicts": sum(row["verdict"] == "conflict" for row in data["conflicts"]),
            "review": sum(row["verdict"] == "review" for row in data["conflicts"]),
            # 批次 16b-Q2：限定范围时，judged 只数两边文档都可读的判断结果；待判断候选是全局数，不给出（null）
            "judged": len(link_store.judged()) if scope is None else sum(
                1 for row in link_store.conflicts() if row.version_a in published and row.version_b in published),
            "candidates": queries.conflict_candidate_count() if scope is None else None, "jobs": job_status}})

    @app.post("/api/links/extract")
    def extract_links(body: _ExtractRequest) -> JSONResponse:
        try:
            count = (int(link_jobs.enqueue_references(body.version_id, force=body.force)) if body.version_id
                     else link_jobs.enqueue_missing())
        except KeyError:
            raise HTTPException(404)
        except ValueError as exc:
            return response({"error": str(exc)}, 400)
        return response({"queued": count}, 202)

    @app.post("/api/links/conflicts")
    def check_conflicts() -> JSONResponse:
        return response({"queued": int(link_jobs.enqueue_conflicts())}, 202)

    @app.get("/api/knowledge")
    def knowledge_status(access: Access = Depends(current)) -> JSONResponse:
        scope = gate.scope(access)
        job_status = scoped_jobs(access, extraction_jobs.status(), "version_id", lambda version: version)
        counts = queries.knowledge_counts(scope=scope)
        return response({"published": store.document_stats(scope)["by_status"].get("published", 0),
                         "extracted": counts["extracted"], "entities": counts["entities"],
                         "events": counts["events"], "jobs": job_status})

    @app.post("/api/knowledge/extract")
    def extract(body: _ExtractRequest) -> JSONResponse:
        try:
            count = (int(extraction_jobs.enqueue(body.version_id, force=body.force)) if body.version_id
                     else extraction_jobs.enqueue_missing())
        except KeyError:
            raise HTTPException(404)
        except ValueError as exc:
            return response({"error": str(exc)}, 400)
        return response({"queued": count}, 202)

    @app.get("/api/timeline")
    def timeline(review: str = "1", start: str = "", end: str = "", limit: int = 2000, series: str = "",
                 access: Access = Depends(current)) -> JSONResponse:
        """start、end 为 ISO 日期（可省略）；limit 取 1–TIMELINE_LIMIT_MAX。参数无效 → 400。
        批次 23c：series 为系列 ID 时只含该系列可读成员中的事件（见 series_scope）。"""
        try:
            first = date.fromisoformat(start) if start else None
            last = date.fromisoformat(end) if end else None
            if not 1 <= limit <= TIMELINE_LIMIT_MAX or (first and last and first > last):
                raise ValueError
        except ValueError:
            return response({"error": "时间轴参数无效"}, 400)
        return response(queries.timeline(include_review=review != "0", start=first, end=last, limit=limit,
                                         scope=series_scope(access, series)))

    @app.get("/api/graph")
    def graph(review: str = "1", events: str = "1", focus: str = "", depth: int = 1, links: str = "1") -> JSONResponse:
        if focus and not 1 <= depth <= 3:
            return response({"error": "图谱深度必须在 1 至 3 之间"}, 400)
        documents = {row["version_id"]: row["title"] for row in store.documents() if row["status"] == "published"}
        view = link_views() if links != "0" else None
        try:
            data = build_graph(documents, reviewed.entities(), reviewed.mentions(), reviewed.events(),
                               include_review=review != "0", include_events=events != "0", focus=focus or None, depth=depth,
                               doc_edges=document_edges(view["links"], view["conflicts"],
                                                        include_review=review != "0") if view is not None else ())
        except ValueError:
            raise HTTPException(404)
        return response(data)

    @app.get("/api/entity-graph")
    def entity_graph(review: str = "1", focus: str = "", depth: int = 1, limit: int = 300, series: str = "",
                     access: Access = Depends(current)) -> JSONResponse:
        """批次 23c：series 为系列 ID 时只用该系列可读成员中的提及、事件与关系构图（见 series_scope）。"""
        if not 20 <= limit <= 1000 or (focus and not 1 <= depth <= 3):
            raise HTTPException(status_code=400, detail="图谱参数超出范围")
        scope = series_scope(access, series)
        try:
            data = queries.entity_graph(include_review=review != "0", focus=focus or None, depth=depth, limit=limit,
                                        scope=scope)
        except ValueError as error:
            raise HTTPException(status_code=404, detail="未找到该资源") from error
        return response(data)

    @app.get("/api/entities/{entity_id}")
    def entity_detail(entity_id: str, access: Access = Depends(current)) -> JSONResponse:
        data = queries.entity_detail(entity_id, limit=DETAIL_LIMIT, scope=gate.scope(access))
        if data is None:
            raise HTTPException(404)
        return response(data)

    static = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static, check_dir=False), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static / "index.html", headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'",
            "X-Content-Type-Options": "nosniff",
        })

    return app
