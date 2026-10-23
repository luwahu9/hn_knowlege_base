"""应用层端口（接口契约）。适配器层实现这些接口，用例层只依赖这里。

通用约定：
- 所有方法同步定义；是否在适配器里用线程池或 async 包装由 apps/worker 决定。
- 临时性错误（超时、连接失败、5xx）抛 TransientError，调用方可重试；
  永久性错误（参数非法、文件损坏、4xx）抛 PermanentError，不应重试。
- 重复调用的行为以各方法 docstring 为准。标明幂等的方法（如 BlobStore.put 同内容、TaskRepo.enqueue、Indexer.publish）
  重复调用不得产生重复数据；明确要求报错的（如重复 create、重复 complete）必须报错。
- 任何 ID、哈希、键都是不透明字符串，适配器不得解析其含义。
"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from core.domain.access_policy import AclSnapshot, Principal
from core.domain.version_fsm import Transition, VersionState


class TransientError(Exception):
    """可重试的临时错误。"""


class PermanentError(Exception):
    """不应重试的永久错误。"""


class ConcurrencyError(Exception):
    """乐观并发冲突：期望的当前状态与实际不符。"""


class UnsupportedFormatError(PermanentError):
    """文件格式不在支持清单内（例如暂不支持的 PowerPoint）。调用方应把该版本送入异常队列并写明原因，不算转换失败。"""


# ---------------------------------------------------------------- 1. 存储
@runtime_checkable
class BlobStore(Protocol):
    """私有对象存储。键由调用方给出；内容寻址由调用方决定（例如 sha256/xx/yy...）。"""

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        """写入并返回内容的 sha256 十六进制串。键已存在且内容相同则直接返回，内容不同抛 PermanentError。"""

    def get(self, key: str) -> bytes:
        """读取。键不存在抛 PermanentError。"""

    def exists(self, key: str) -> bool: ...

    def delete_prefix(self, prefix: str) -> int:
        """批次 20b：删除键以 prefix 开头的全部对象，返回删除的个数（没有时为 0）。
        prefix 必须以 "/" 结尾，且去掉末尾 "/" 后是合法的键，否则 PermanentError（防止误删整个存储）。"""

    def signed_url(self, key: str, *, ttl_seconds: int) -> str:
        """签发临时下载链接。ttl_seconds 必须在 (0, 3600] 之间，否则抛 PermanentError；键不存在抛 PermanentError。
        返回非空字符串。权限检查在调用本方法之前由用例层完成；适配器不做权限判断。"""


# ---------------------------------------------------------------- 2. 文档转换
@dataclass(frozen=True)
class ConvertResult:
    markdown_key: str           # BlobStore 中 Markdown 的键
    structure_key: str          # Docling 结构 JSON 的键
    page_count: int
    ocr_used: bool
    parser: str                 # 例如 "docling"
    parser_version: str
    config_hash: str            # 转换配置的哈希，参与幂等


@runtime_checkable
class Converter(Protocol):
    """把原件转成 Markdown + 结构 JSON（页码与位置信息）。"""

    def convert(self, source_key: str, *, filename: str, config_hash: str, ocr: bool) -> ConvertResult:
        """把 BlobStore 中 source_key 处的原件转换为 Markdown 与结构 JSON，并写回 BlobStore。
        filename 仅用于判断格式（扩展名）和传给转换服务，不参与缓存键。
        幂等靠缓存而不是重算：转换结果不保证逐字节可复现（坐标可能有极小差异），
        同一 (原件内容哈希, config_hash, ocr) 第二次调用必须直接返回已存档的结果，不再请求转换服务。
        格式不在支持清单 → UnsupportedFormatError；超时、连接失败、5xx → TransientError；
        转换服务明确报告失败、4xx、返回内容缺失 → PermanentError。"""


@dataclass(frozen=True)
class OcrBlock:
    text: str
    bbox: tuple[float, float, float, float]   # 归一化坐标 x0,y0,x1,y1，范围 [0,1]
    kind: str                                  # "text" | "table" | "formula" | "figure"


@dataclass(frozen=True)
class OcrPage:
    page_no: int                               # 从 1 开始
    blocks: tuple[OcrBlock, ...]
    markdown: str


@runtime_checkable
class OcrEngine(Protocol):
    """版面感知的 OCR（对应 GPUStack 里的 PaddleOCR-VL）。供 Converter 的扫描件路径调用。"""

    def recognize(self, page_image_key: str) -> OcrPage: ...


# ---------------------------------------------------------------- 3. 模型网关
class Role(str, Enum):
    """模型角色别名。具体模型在 GPUStack / 配置中映射，代码里只出现角色。"""

    QA = "qa"
    EXTRACT = "extract"
    REVIEW = "review"
    TRIAGE = "triage"
    REWRITE = "rewrite"          # 批次 21a：多轮改写与关键词抽取（短输入、短输出，可配较小的模型）


@dataclass(frozen=True)
class LlmUsage:
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class LlmResult:
    text: str
    parsed: Any | None            # 请求了 json_schema 且解析成功时的对象，否则 None
    usage: LlmUsage
    model_alias: str              # 实际使用的模型名，供审计和 manifest 记录


@runtime_checkable
class LlmGateway(Protocol):
    def complete(
        self,
        role: Role,
        messages: Sequence[Mapping[str, str]],
        *,
        json_schema: Mapping[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        caller: str,
    ) -> LlmResult:
        """caller 用于限流与计费归因（例如 "ingest.review"）。
        json_schema 给出时，模型输出能解析为 JSON 对象则 parsed 为该对象，否则 parsed 为 None，text 保留原文，
        不抛异常。适配器不做逐字段的 schema 校验，字段是否齐全、类型是否正确由用例层校验（批次 7b 裁决）。
        网络超时抛 TransientError。"""

    def embed(self, texts: Sequence[str], *, caller: str) -> list[list[float]]:
        """返回与输入等长、顺序一致的向量列表。"""

    def rerank(self, query: str, passages: Sequence[str], *, caller: str) -> list[float]:
        """返回与 passages 等长、顺序一致的相关度分数（越大越相关）。"""


# ---------------------------------------------------------------- 4. 检索引擎（RAGFlow）
@dataclass(frozen=True)
class Chunk:
    chunk_id: str                  # 稳定 ID：版本 ID + 序号，重复发布时保持不变
    text: str
    page_start: int | None
    page_end: int | None
    section_path: str | None       # 章节路径，短文可为 None
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IndexReceipt:
    version_id: str
    engine_doc_id: str             # 检索引擎里的文档 ID，本地任务表保存它
    chunk_count: int
    chunk_map: Mapping[str, str] = field(default_factory=dict)   # 我们的 chunk_id → 引擎分块 ID


@dataclass(frozen=True)
class VerifyResult:
    indexed: bool                  # 引擎侧状态为“已解析/已索引”
    chunk_count: int
    sample_hit: bool               # 用该版本自身的片段做抽样检索能命中


@dataclass(frozen=True)
class Hit:
    """检索命中。适配器层的 chunk_id 是**引擎分块 ID**，page_start / page_end / section_path 为 None；
    用例层通过发布时保存的 IndexReceipt.chunk_map 映射回我们的分块，并补上页码与章节。"""

    version_id: str
    chunk_id: str
    text: str
    score: float
    page_start: int | None
    page_end: int | None
    section_path: str | None


@runtime_checkable
class Indexer(Protocol):
    """检索引擎。每个发布版本在引擎中对应**恰好一个**文档，以 version_id 命名。
    用户组标签 group_tags：非空字符串，不得包含 "|"，否则 ValueError（在任何网络请求之前检查）。"""

    def publish(self, version_id: str, chunks: Sequence[Chunk], group_tags: Sequence[str]) -> IndexReceipt:
        """写入该版本的全部分块并设置用户组标签。chunks 不得为空，chunk_id 不得重复，否则 ValueError。
        幂等方式：若引擎中已存在该版本的文档（无论是否完整，例如上次中途失败），先删除再重建，
        结束时引擎中该版本恰好一个文档、恰好 len(chunks) 个分块。
        写入成功不等于可检索，调用方必须再调用 verify。"""

    def verify(self, version_id: str) -> VerifyResult:
        """indexed：引擎中存在该版本文档且分块数 > 0（不得依赖引擎的解析状态字段，手动写入分块时该状态不会变化）；
        chunk_count：引擎报告的分块数；sample_hit：用该版本第一个分块的文本检索（限定在该文档内）能命中该文档。
        文档不存在 → VerifyResult(False, 0, False)，不抛异常。"""

    def set_group_tags(self, version_ids: Sequence[str], group_tags: Sequence[str]) -> None:
        """仅更新用户组标签，不得触发重新解析或重新向量化。任一版本在引擎中不存在 → PermanentError（数据不同步必须暴露）。"""

    def search(self, query: str, *, group_tags: Sequence[str], top_k: int) -> list[Hit]:
        """group_tags 为空时必须返回空列表且不发出任何请求（默认拒绝），不得退化成不过滤。
        过滤发生在检索之前（前置过滤），只返回带有任一给定用户组标签的文档的分块，按相关度降序，至多 top_k 条。"""

    def remove(self, version_id: str) -> None:
        """从检索范围中移除该版本。不存在时静默成功。"""


# ---------------------------------------------------------------- 5. 连线候选
@dataclass(frozen=True)
class Candidate:
    doc_id: str
    score: float
    via: str                       # "vector" | "entity" | "citation"


@runtime_checkable
class CandidateSource(Protocol):
    """为一个已发布版本生成可能有关系的其他文档。以系统身份运行，不做权限过滤；
    权限在读取边的时候统一过滤。"""

    def candidates(self, version_id: str, *, limit: int) -> list[Candidate]:
        """结果不含自身，同一 doc_id 只出现一次（保留最高分），按 score 降序，至多 limit 条。"""


# ---------------------------------------------------------------- 6. 图谱读取
class NodeType(str, Enum):
    DOCUMENT = "document"
    ENTITY = "entity"
    EVENT = "event"


@dataclass(frozen=True)
class NodeRef:
    type: NodeType
    id: str


@dataclass(frozen=True)
class GraphEdge:
    id: str
    source: NodeRef
    target: NodeRef
    kind: str                      # "cites" | "supersedes" | "conflicts" | "series" | "parent_of" | "belongs_to" | "mentions" ...
    origin: str                    # "model" | "rule" | "human"
    confidence: float | None


@dataclass(frozen=True)
class Subgraph:
    nodes: tuple[NodeRef, ...]
    edges: tuple[GraphEdge, ...]
    truncated: bool                # 因 limit 被截断


@runtime_checkable
class GraphRepo(Protocol):
    def neighbors(
        self, principal: Principal, node: NodeRef, *, depth: int, edge_kinds: Sequence[str] | None, limit: int
    ) -> Subgraph:
        """返回以 node 为中心、深度不超过 depth（1–3）的局部子图。
        权限在实现内部强制执行：任何一端是文档/事件且 principal 不可读，则该边与该节点都不返回；
        不可读节点的存在、标题、数量都不得泄露（包括 truncated 的判断）。"""

    def upsert_edge(self, edge: GraphEdge, *, evidence_ids: Sequence[str]) -> None:
        """origin == "human" 的边不会被 origin != "human" 的写入覆盖或删除。"""

    def remove_edge(self, edge_id: str, *, by_origin: str) -> None:
        """by_origin 为 "model" 或 "rule" 时不能删除 origin == "human" 的边（抛 PermanentError）。"""


# ---------------------------------------------------------------- 7. 仓储
@dataclass(frozen=True)
class VersionRecord:
    version_id: str
    source_id: str
    state: VersionState
    artifact_hash: str | None
    created_at: datetime


@runtime_checkable
class VersionRepo(Protocol):
    def create(self, version_id: str, source_id: str, *, initial_state: VersionState, created_at: datetime) -> VersionRecord:
        """登记新版本，artifact_hash 为 None。version_id 已存在抛 PermanentError。"""

    def get(self, version_id: str) -> VersionRecord:
        """不存在抛 PermanentError。"""

    def history(self, version_id: str) -> list[Transition]:
        """该版本的全部状态转移审计记录，按写入顺序。版本不存在抛 PermanentError；尚无转移返回空列表。"""

    def apply(self, version_id: str, expected_state: VersionState, transition: Transition) -> VersionRecord:
        """在一个数据库事务内：校验当前状态等于 expected_state（否则抛 ConcurrencyError），
        更新状态并写入审计记录。这是修改版本状态的唯一入口。
        校验顺序：版本不存在 → PermanentError；transition.from_state != expected_state → ValueError（调用方程序错误）；
        当前状态 != expected_state → ConcurrencyError。任何异常都不得留下部分写入（状态和审计要么都写，要么都不写）。
        仓储不重新判断转移是否合法，合法性由领域层 apply_event 保证。"""


MAX_TASK_ATTEMPTS = 3


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class Task:
    task_id: str
    idempotency_key: str          # 形如 f"{version_id}:{step}:{section_or_all}:{config_hash}"
    kind: str
    payload: Mapping[str, Any]
    attempt: int                  # 从 1 开始
    status: TaskStatus
    worker_id: str | None = None  # 当前持有租约的 worker（仅 RUNNING 时有值）
    last_error: str | None = None # 最近一次 fail 记录的错误
    # 批次 15c 增加（TaskQueue 使用）：
    progress: str | None = None           # 执行者通过 heartbeat 报告的进度；领取时清空
    enqueued_at: datetime | None = None   # 入队时刻（时钟值），之后不变
    finished_at: datetime | None = None   # 进入 DONE / FAILED 的时刻；其他状态为 None


@runtime_checkable
class TaskRepo(Protocol):
    """任务队列。实现类的构造函数必须接受一个时钟 `clock: Callable[[], datetime]`，所有租约计算都用它，
    不得直接读系统时间（PostgreSQL 实现把时钟值作为 SQL 参数传入，而不是用 now()）。这样契约测试才能控制时间。"""

    def enqueue(self, idempotency_key: str, kind: str, payload: Mapping[str, Any]) -> Task:
        """新任务：QUEUED、attempt=1。相同 idempotency_key 已存在（任何状态，包括 DONE、FAILED）则原样返回已有任务，
        忽略本次的 kind 和 payload，不新建。"""

    def get(self, task_id: str) -> Task:
        """不存在抛 PermanentError。返回的 payload 是副本，调用方修改它不影响存储。"""

    def claim(self, kinds: Sequence[str], *, worker_id: str, lease_seconds: int) -> Task | None:
        """原子领取一个任务（等价于 SELECT ... FOR UPDATE SKIP LOCKED），并发调用不会领到同一个任务。
        - 可领取：kind 在 kinds 中，且状态为 QUEUED，或状态为 RUNNING 但租约已到期（到期时刻 <= 当前时钟）。
        - 顺序：按入队先后，先入先出。
        - 领取后：RUNNING，worker_id 为领取者，租约到期时刻 = 当前时钟 + lease_seconds。
        - 租约到期被重新领取时 attempt + 1；若 + 1 后超过 MAX_TASK_ATTEMPTS，则该任务置 FAILED
          （last_error = "lease expired"，attempt 保持原值即 MAX_TASK_ATTEMPTS，worker_id 清空），
          不返回它，继续找下一个可领取的任务。attempt 的含义是“实际开始执行的次数”，永远不超过 MAX_TASK_ATTEMPTS。
        - kinds 为空或没有可领取任务 → None。lease_seconds <= 0 → ValueError。"""

    def complete(self, task_id: str, *, worker_id: str) -> None:
        """置 DONE。任务不存在 → PermanentError；状态不是 RUNNING、worker_id 不是当前持有者、或租约已到期 → ConcurrencyError。"""

    def fail(self, task_id: str, *, worker_id: str, error: str, retryable: bool) -> Task:
        """记录 last_error。retryable 且 attempt < MAX_TASK_ATTEMPTS 时回到 QUEUED（attempt + 1，worker_id 清空）；
        否则置 FAILED。所有权检查与 complete 相同。返回更新后的任务。"""


@runtime_checkable
class TaskQueue(TaskRepo, Protocol):
    """批次 15c：可供多个进程共用的任务队列（TaskRepo 加上续租、进度与查询）。规格见 docs/spec-26-task-queue.md。

    TaskRepo 各方法的语义不变，另外：enqueue 记录 enqueued_at；complete、fail 置 FAILED 时记录 finished_at，
    fail 回到 QUEUED 时 finished_at 为 None；claim 清空 progress。"""

    def heartbeat(self, task_id: str, *, worker_id: str, lease_seconds: int, progress: str | None = None) -> None:
        """续租：租约到期时刻 = 当前时钟 + lease_seconds；progress 不为 None 时记录为当前进度。
        lease_seconds <= 0 → ValueError；任务不存在 → PermanentError；所有权检查与 complete 相同（ConcurrencyError）。"""

    def tasks(self, kind: str, *, statuses: Collection[TaskStatus] | None = None, limit: int = 1000) -> list[Task]:
        """kind 的任务，按入队先后；statuses 给出时只取这些状态；最多返回最早的 limit 条。limit < 1 → ValueError。"""

    def count(self, kind: str, status: TaskStatus) -> int:
        """kind 中处于 status 的任务数。"""

    def find(self, idempotency_key: str) -> Task | None:
        """按幂等键取任务（批次 15d）；没有为 None。"""

    def latest_per(self, kind: str, field: str) -> list[Task]:
        """按 payload[field] 的值分组（payload 没有该键的任务不计），每组取最近入队的一个任务；
        结果按这些任务的入队先后排序。"""


@dataclass(frozen=True)
class AclAuditEntry:
    target_type: str                    # "folder_parent" | "folder_acl" | "doc_override"
    target_id: str
    value: frozenset[str] | str | None  # 用户组集合；folder_parent 时为父文件夹 id
    actor: str
    reason: str | None


@runtime_checkable
class AclRepo(Protocol):
    """所有写方法：actor 去空白后为空 → ValueError，在其他任何检查之前进行，不写入任何东西。
    actor、reason 原样保存（去空白只用于校验）。每次成功调用都追加一条审计记录，即使值与当前相同。"""

    def add_folder(self, folder_id: str, parent_id: str | None, *, actor: str) -> None:
        """新建文件夹。folder_id 已存在 → PermanentError；parent_id 不为 None 且不存在 → PermanentError。
        写审计（target_type="folder_parent"，value=parent_id）。"""

    def snapshot(self) -> AclSnapshot:
        """读取当前文件夹树与权限设置的一致性快照。快照是不可变副本：之后的修改不影响已取得的快照。"""

    def set_folder_acl(self, folder_id: str, group_ids: frozenset[str] | None, *, actor: str) -> None:
        """None 表示取消显式设置（改为继承）。文件夹不存在 → PermanentError。写审计（target_type="folder_acl"）。"""

    def set_doc_override(self, doc_id: str, group_ids: frozenset[str] | None, *, actor: str, reason: str) -> None:
        """覆盖必须填写 reason（去空白后非空），否则抛 ValueError；取消覆盖（None）同样要求 reason。
        doc_id 不要求预先存在（文档表不归本仓储管）。写审计（target_type="doc_override"）。"""

    def history(self, target_id: str) -> list[AclAuditEntry]:
        """与该 id 相关的全部审计记录（任何 target_type），按写入顺序；没有则空列表。"""
