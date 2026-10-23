"""文档登记端口与知识事件（发件箱）。规格见 docs/spec-24-registry.md。

两个实现：apps/web/store.py 的 KbStore（JSON 文件，开发与测试）和 core/adapters/pg_registry.py 的
PgRegistry（PostgreSQL）。两者必须通过 tests/contracts/registry.py 的同一套契约测试。
"""
from __future__ import annotations

import copy
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from core.app.retrieval import ChunkLookup
from core.domain.permissions import Visibility

DOCUMENT_PUBLISHED = "document_published"
DOCUMENT_REMOVED = "document_removed"
EXTRACTION_DONE = "extraction_done"
LINKS_DONE = "links_done"
DECISION_CHANGED = "decision_changed"
CONFLICTS_JUDGED = "conflicts_judged"          # 批次 15b：一轮冲突判断保存了至少一条结果，payload {"judged": 条数}
EVENT_KINDS = frozenset({DOCUMENT_PUBLISHED, DOCUMENT_REMOVED, EXTRACTION_DONE, LINKS_DONE, DECISION_CHANGED,
                         CONFLICTS_JUDGED})


@runtime_checkable
class DocumentRegistry(Protocol):
    """文档、分块、文件夹、未收录问题与知识事件的唯一登记处。

    文档记录是 dict（字段见 apps/web/jobs.py），实现按原样保存与返回，只补一个 "folder" 键（规范路径，缺省 ""）。
    所有返回值都是副本，调用方修改不影响登记处。
    """

    @property
    def dataset_id(self) -> str: ...

    @property
    def dataset_name(self) -> str: ...

    @property
    def group_tag(self) -> str: ...

    @property
    def startup_versions(self) -> frozenset[str]: ...

    def documents(self) -> list[dict[str, Any]]:
        """全部文档记录，按保存先后顺序。"""

    def detail(self, version_id: str) -> dict[str, Any] | None:
        """{"document": 记录, "chunks": 分块行列表（未发布为 []）}；不存在为 None。"""

    def save_document(self, record: dict[str, Any], chunks: list[dict[str, Any]]) -> None:
        """追加一个新版本。version_id 已存在 → ValueError("不得覆盖已有文档记录")，登记处不变。
        record["folder"] 缺省为 ""，先规范化（非法 → ValueError，登记处不变），并自动建立其全部祖先文件夹。
        status 为 "published" 时保存分块，并在同一事务中追加 document_published 事件，
        payload 为 {"folder": 规范路径, "title": 标题}；其他状态不保存分块、不产生事件。"""

    def next_version_number(self) -> int:
        """分配一个新的版本序号（批次 15c）：严格大于已分配过的任何序号与已登记的任何版本号中的序号，
        跨进程不重复、不复用（版本号为 f"v{序号:02d}_{内容哈希前 10 位}"）。"""

    def set_review(self, version_id: str, review: dict[str, Any] | None) -> dict[str, Any]:
        """设置文档记录的人工审核状态（批次 15c）：记录的 "review" 键设为 review 的副本；None 表示去掉该键。
        返回更新后的记录副本。版本不存在 → KeyError。只改这一个键，其余字段不变；不产生知识事件。"""

    def set_status(self, version_id: str, status: str, entry: dict[str, Any], *,
                   chunk_map: Mapping[str, str] | None = None, engine_doc_id: str | None = None) -> dict[str, Any]:
        """批次 20a：在 published、withdrawn、superseded 之间改变文档状态，返回更新后的记录副本。
        - 版本不存在 → KeyError；当前状态或目标状态不在三者之中、或与当前状态相同 → ValueError；登记处不变。
        - 记录的 "lifecycle" 列表（没有则新建）末尾追加 entry 的副本；"status" 改为 status。
        - 目标为 published 时 chunk_map（chunk_id → 新的引擎分块 ID）必须覆盖该版本已保存的全部分块，否则 ValueError；
          分块行的 engine_chunk_id 按它更新，记录的 engine_doc_id 改为给出的值；其他目标状态不得给出两者（ValueError）。
        - 同一事务中追加知识事件：离开 published → document_removed，payload {"folder","title","status": 新状态}；
          进入 published → document_published，payload {"folder","title","restored": true}。
        - 分块始终保留（stored_chunks 可取），detail 仍只在 published 时返回分块。"""

    def stored_chunks(self, version_id: str) -> list[dict[str, Any]]:
        """批次 20a：该版本保存过的全部分块行（与发布时保存的相同，engine_chunk_id 为最近一次发布的），按原顺序；
        不论当前状态。没有分块（从未发布）或版本不存在 → []。"""

    def purge(self, version_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        """批次 20b：彻底删除登记处中的内容，返回墓碑记录（core.domain.purge.tombstone）。
        - 版本不存在 → KeyError；当前状态不是 withdrawn 或 superseded → ValueError；登记处不变。
        - 删除该版本保存的全部分块（stored_chunks 之后为 []）；记录替换为墓碑（只剩版本号、文件夹、文件名、标题、
          导入时间、来源、status "purged" 与追加了 entry 的 lifecycle）。
        - 同一事务中追加 document_removed 事件，payload {"folder","title","status": "purged"}。
        - 墓碑仍在 documents()、detail() 中（detail 的 chunks 为 []）；不参与 find_by_digest 与 find_by_hash，
          文档库分页（document_page）只在 status 为 "purged" 时列出它们。"""

    def switch_index(self, dataset_id: str, dataset_name: str,
                     mapping: Mapping[str, tuple[str, Mapping[str, str]]]) -> None:
        """批次 21b：切换检索数据集（重建完成后的提交点）。mapping：已发布版本 → (引擎文档 ID, chunk_id → 引擎分块 ID)。
        在一个事务（JSON 实现为一次原子写入）中：
        - 当前已发布版本的集合必须恰好等于 mapping 的键，且每个 chunk_map 恰好覆盖该版本的全部 chunk_id，否则 ValueError，不做任何修改；
        - 每个已发布版本的记录 engine_doc_id 与各分块行的 engine_chunk_id 换成新值（其他字段、其他状态的版本不变）；
        - 知识库元数据 dataset_id、dataset_name 换成新值；本实例的 dataset_id / dataset_name 随之改变。
        dataset_id 或 dataset_name 为空 → ValueError。不产生知识事件。
        数据库实现中，其他进程启动时读取的 dataset_id 与库中不一致后，save_document、set_status、purge 抛 PermanentError
        （“检索数据集已切换，请重启进程”），不写入任何内容。"""

    def find_by_digest(self, sha256: str) -> str | None:
        """原件 sha256 对应的已登记版本（批次 15d）：version_id 末 10 位等于 sha256 前 10 位（小写）的记录中最早保存的一个；
        已彻底删除（status "purged"，批次 20b）的不算，同一文件可以重新导入。没有为 None。sha256 不是 64 位十六进制 → ValueError。（版本号的哈希部分即原件哈希前缀，种子文档同样适用。）"""

    def document_page(self, scope: Visibility | None = None, *, status: str | None = None, query: str | None = None,
                      offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """文档库分页（批次 16b）：{"rows": 记录副本列表, "total": 符合条件的总数}。语义见 page_documents。"""

    def document_stats(self, scope: Visibility | None = None) -> dict[str, Any]:
        """{"documents": 数量, "by_status": {状态: 数量}, "chunks": 已发布文档的 chunk_count 之和}，只数 scope 内的文档。"""

    def folder_counts(self) -> dict[str, int]:
        """{规范文件夹路径: 直接位于该文件夹的文档数}（全部文档，不含为 0 的文件夹）。"""

    def folders_of(self, version_ids: Iterable[str]) -> dict[str, str]:
        """{version_id: 所在文件夹}；不存在的版本不出现在结果中。"""

    def records(self, version_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """批次 18a：{version_id: 文档记录（与 documents() 中的同一条相同，深拷贝）}；不存在的版本不出现。
        只取给出的版本（跨来源问答为命中的几份文档取标题与时间段），不复制全部记录。"""

    def find_by_hash(self, content_hash: str) -> str | None:
        """content_hash 相同的已发布文档中最早保存的 version_id；没有为 None。"""

    def context(self) -> tuple[ChunkLookup, dict[str, str], dict[str, str]]:
        """一次请求用的上下文：(分块查询, version→title, version→filename)。分块查询只能查到已发布文档的分块。"""

    def record_unanswered(self, question: str, status: str) -> None: ...

    def unanswered(self) -> list[dict[str, str]]:
        """[{"time","question","status"}]，按记录先后顺序。"""

    def ensure_folder(self, path: str) -> str:
        """规范化并建立文件夹及其全部祖先，返回规范路径；已存在不报错。根 "" 永远存在。"""

    def folders(self) -> list[str]:
        """全部已建立的文件夹规范路径（不含根 ""），按字符串升序。"""

    def append_event(self, kind: str, version_id: str | None = None, payload: dict[str, Any] | None = None) -> int:
        """追加知识事件，返回其序号 seq（从 1 起严格递增，不复用）。kind 不在 EVENT_KINDS → ValueError。"""

    def events(self, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """seq > after 的事件，按 seq 升序，最多 limit 条（limit < 1 → ValueError）。
        每条 {"seq": int, "kind": str, "version_id": str 或 None, "payload": dict, "time": ISO 时间}。"""

    def last_event_seq(self) -> int:
        """最大的事件序号；没有事件为 0（批次 15b：查询结果缓存以它为版本号）。"""

    def offset(self, subscriber: str) -> int:
        """订阅者已处理到的 seq；从未提交为 0。"""

    def commit_offset(self, subscriber: str, seq: int) -> None:
        """记录订阅者进度。seq 小于当前进度或大于最大事件序号 → ValueError；等于当前进度为无操作。"""


LIFECYCLE = frozenset({"published", "withdrawn", "superseded"})


def check_status_change(current: str, status: str, chunk_ids: Iterable[str], chunk_map: Mapping[str, str] | None,
                        engine_doc_id: str | None) -> None:
    """set_status 的参数检查（两个实现共用）：不合规 → ValueError。"""
    if current not in LIFECYCLE or status not in LIFECYCLE or current == status:
        raise ValueError("只能在已发布、已撤下、已替代之间改变状态")
    if status == "published":
        if chunk_map is None or engine_doc_id is None or not set(chunk_ids) <= set(chunk_map):
            raise ValueError("重新发布时必须给出全部分块的引擎 ID 与引擎文档 ID")
    elif chunk_map is not None or engine_doc_id is not None:
        raise ValueError("只有重新发布时才能给出引擎 ID")


def check_switch(dataset_id: str, dataset_name: str, published: Mapping[str, Iterable[str]],
                 mapping: Mapping[str, tuple[str, Mapping[str, str]]]) -> None:
    """switch_index 的参数检查（两个实现共用）。published：已发布版本 → 其全部 chunk_id。不合规 → ValueError。"""
    if not dataset_id or not dataset_name:
        raise ValueError("数据集 ID 与名称不能为空")
    if set(published) != set(mapping):
        raise ValueError("已发布版本与新数据集中的版本不一致，请先完成同步")
    for version, chunk_ids in published.items():
        engine_doc_id, chunk_map = mapping[version]
        if not engine_doc_id or set(chunk_map) != set(chunk_ids) or not all(chunk_map.values()):
            raise ValueError(f"版本 {version} 的分块映射不完整")


def status_event(record: Mapping[str, Any], status: str) -> tuple[str, dict[str, Any]]:
    """状态变化对应的知识事件 (kind, payload)。"""
    if status == "published":
        return DOCUMENT_PUBLISHED, {"folder": record["folder"], "title": record["title"], "restored": True}
    return DOCUMENT_REMOVED, {"folder": record["folder"], "title": record["title"], "status": status}


Handler = Callable[[dict[str, Any]], None]


class EventDispatcher:
    """把知识事件按序分发给订阅者；每个订阅者独立进度，至少一次投递，处理器须幂等。

    - subscribe 必须在 start 之前调用；同名订阅者重复订阅 → ValueError。
    - run_once：对每个订阅者，从其进度之后读取事件（每次最多 batch 条，读到没有为止）；
      kind 在其 kinds 内的调用处理器，不在的直接跳过；每条处理成功或跳过后立即提交进度。
      处理器抛异常：该订阅者停在这条事件（不提交），记录错误，本轮不再处理它，下轮从这条重试；其他订阅者照常。
      某订阅者之后处理成功，其错误被清除。返回本轮调用处理器成功的次数。
    - start 启动后台线程：notify() 立即唤醒，否则每 poll_seconds 秒轮询一次；close 停止并等待线程结束。
    """

    def __init__(self, registry: DocumentRegistry, *, poll_seconds: float = 1.0, batch: int = 100) -> None:
        if batch < 1 or poll_seconds <= 0:
            raise ValueError("batch 与 poll_seconds 必须大于零")
        self._registry = registry
        self._poll_seconds = poll_seconds
        self._batch = batch
        self._subscribers: dict[str, tuple[frozenset[str], Handler]] = {}
        self._errors: dict[str, str | None] = {}
        self._lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False

    def subscribe(self, name: str, kinds: Iterable[str], handler: Handler) -> None:
        selected = frozenset(kinds)
        with self._lock:
            if self._started or not name.strip() or name in self._subscribers or not selected or not selected <= EVENT_KINDS:
                raise ValueError("订阅者名称、事件类型或订阅时机无效")
            self._subscribers[name] = (selected, handler)
            self._errors[name] = None

    def run_once(self) -> int:
        with self._run_lock:
            with self._lock:
                subscribers = list(self._subscribers.items())
            return sum(self._drain(name, kinds, handler) for name, (kinds, handler) in subscribers)

    def _drain(self, name: str, kinds: frozenset[str], handler: Handler) -> int:
        total = 0
        seq = 0
        try:
            cursor = self._registry.offset(name)
            while True:
                events = self._registry.events(cursor, self._batch)
                if not events:
                    return total
                for event in events:
                    seq = event["seq"]
                    if event["kind"] in kinds:
                        handler(event)
                        total += 1
                    self._registry.commit_offset(name, seq)
                    cursor = seq
                    with self._lock:
                        self._errors[name] = None
        except Exception as exc:
            with self._lock:
                self._errors[name] = f"事件 {seq}：{type(exc).__name__}: {exc}"
            return total

    def status(self) -> dict[str, dict[str, Any]]:
        """{订阅者: {"offset": int, "error": str 或 None}}。"""
        with self._lock:
            return {name: {"offset": self._registry.offset(name), "error": self._errors[name]}
                    for name in self._subscribers}

    def notify(self) -> None:
        self._wake.set()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._started = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._work, name="haina-events", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def _work(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            self.run_once()
            self._wake.wait(self._poll_seconds)


PAGE_LIMIT_MAX = 500


def recent_key(row: Mapping[str, Any]) -> float:
    """文档库的排序键：导入时刻（越新越前）；与批次 7c 起的 _recent 相同，时刻相同保持保存先后。"""
    return -datetime.fromisoformat(row["imported_at"]).timestamp()


def page_documents(rows: Iterable[Mapping[str, Any]], scope: Visibility | None, *, status: str | None,
                   query: str | None, offset: int, limit: int) -> dict[str, Any]:
    """rows 按保存先后给出。筛选：scope 为 None 或 scope.contains(version_id, folder)；status 给出时相等，
    不给出时不含已彻底删除（"purged"，批次 20b）的墓碑；
    query 给出（非空）时为标题或文件名的子串（区分大小写，与批次 7c 相同）。按 recent_key 稳定排序后取
    [offset, offset + limit)。offset < 0 或 limit 不在 1–PAGE_LIMIT_MAX → ValueError。返回的行是深拷贝。"""
    if offset < 0 or not 1 <= limit <= PAGE_LIMIT_MAX:
        raise ValueError("分页参数超出范围")
    selected = [row for row in rows
                if (scope is None or scope.contains(row["version_id"], row.get("folder", "")))
                and (row["status"] == status if status else row["status"] != "purged")       # 批次 20b
                and (not query or query in row["title"] or query in row["filename"])]
    selected.sort(key=recent_key)
    return {"rows": copy.deepcopy(selected[offset:offset + limit]), "total": len(selected)}


def stats_of(rows: Iterable[Mapping[str, Any]], scope: Visibility | None) -> dict[str, Any]:
    selected = [row for row in rows if scope is None or scope.contains(row["version_id"], row.get("folder", ""))]
    return {"documents": len(selected), "by_status": dict(Counter(row["status"] for row in selected)),
            "chunks": sum(row["chunk_count"] for row in selected if row["status"] == "published")}
