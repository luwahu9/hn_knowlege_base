"""批次 21b：检索数据集重建与切换。规格见 docs/spec-39-reindex.md。

流程：start（登记一个空的新数据集）→ sync（把已发布版本写进新数据集，可反复执行以追上变化）→ compare（抽样检索对比）
→ switch（登记处改用新数据集；旧数据集保留为 retired，可回退）→ drop（删除不再需要的数据集）。
回退 = 对 retired 的旧数据集再走一次 start → sync → compare → switch（只重写切换后变化过的版本）。
"""
from __future__ import annotations

import copy
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.app.ports import Chunk, Indexer, PermanentError, TransientError
from core.app.registry import DocumentRegistry
from core.domain.reindex import Probe, chunk_signature, compare_summary, plan_sync, probe_query, sample_evenly

ACTIVE = ("building", "switched", "retired")      # 数据集仍存在、仍需要跟踪的状态


@dataclass(frozen=True)
class DatasetInfo:
    dataset_id: str
    name: str
    embedding_model: str
    document_count: int
    chunk_count: int


class DatasetCatalog(Protocol):
    def info(self, dataset_id: str) -> DatasetInfo | None:
        """数据集信息；不存在（或无权访问）→ None。"""

    def delete(self, dataset_id: str) -> None:
        """删除整个数据集（含其中全部文档与分块）。不存在时静默成功。"""


@dataclass(frozen=True)
class BuiltDoc:
    engine_doc_id: str
    chunk_map: Mapping[str, str]
    signature: str


@dataclass(frozen=True)
class Rebuild:
    rebuild_id: str
    dataset_id: str
    dataset_name: str
    embedding_model: str
    status: str                      # building / switched / retired / dropped
    created_at: str
    created_by: str
    revision: int = 0                # 每次 put_doc / drop_doc / replace_docs 加 1
    compare: Mapping[str, Any] | None = None
    compare_revision: int | None = None
    switched_at: str | None = None
    previous_dataset_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"rebuild_id": self.rebuild_id, "dataset_id": self.dataset_id, "dataset_name": self.dataset_name,
                "embedding_model": self.embedding_model, "status": self.status, "created_at": self.created_at,
                "created_by": self.created_by, "revision": self.revision,
                "compare": copy.deepcopy(dict(self.compare)) if self.compare is not None else None,
                "compare_revision": self.compare_revision, "switched_at": self.switched_at,
                "previous_dataset_id": self.previous_dataset_id}


class RebuildRepo(Protocol):
    """重建记录。约束：dataset_id 唯一（同一数据集只有一条记录）；status 为 building 的记录至多一条。"""

    def rebuilds(self) -> list[Rebuild]:
        """全部记录，按创建先后。"""

    def get(self, rebuild_id: str) -> Rebuild | None: ...

    def create(self, rebuild: Rebuild) -> None:
        """rebuild_id 或 dataset_id 已存在、或已有 building 而新记录也是 building → ValueError。"""

    def update(self, rebuild_id: str, **fields: Any) -> Rebuild:
        """只能改 status、compare、compare_revision、switched_at、previous_dataset_id；改成 building 而已有其他 building → ValueError；
        不存在 → KeyError；未知字段 → ValueError。返回更新后的记录。"""

    def docs(self, rebuild_id: str) -> dict[str, BuiltDoc]:
        """该数据集中已写入的版本 → BuiltDoc。"""

    def put_doc(self, rebuild_id: str, version_id: str, doc: BuiltDoc) -> None:
        """写入或替换一条；revision 加 1。记录不存在 → KeyError。"""

    def drop_doc(self, rebuild_id: str, version_id: str) -> None:
        """删除一条（不存在时也算成功）；revision 加 1。记录不存在 → KeyError。"""

    def replace_docs(self, rebuild_id: str, docs: Mapping[str, BuiltDoc]) -> None:
        """整体替换；revision 加 1。记录不存在 → KeyError。"""


class Tagger(Protocol):
    def tags(self, version_id: str, folder: str) -> tuple[str, ...]: ...


@dataclass
class SyncReport:
    published: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)
    remaining: int = 0               # 因 limit 未处理的待写入版本数

    def to_json(self) -> dict[str, Any]:
        return {"published": list(self.published), "removed": list(self.removed), "unchanged": self.unchanged,
                "failed": list(self.failed), "remaining": self.remaining}


class ReindexService:
    """registry：文档登记处；repo：重建记录；catalog：数据集信息；indexer_for(dataset_id)：该数据集的检索引擎；
    tagger：按当前授权计算标签（与导入、恢复时相同）；clock：时间字符串；new_id：生成重建 ID（缺省 "rb_" + 12 位十六进制）；
    redact：错误说明写入报告前的脱敏函数。"""

    def __init__(self, registry: DocumentRegistry, repo: RebuildRepo, catalog: DatasetCatalog,
                 indexer_for: Callable[[str], Indexer], tagger: Tagger, clock: Callable[[], str], *,
                 new_id: Callable[[], str] = lambda: "rb_" + uuid.uuid4().hex[:12],
                 redact: Callable[[str], str] = lambda text: text) -> None:
        self._registry, self._repo, self._catalog = registry, repo, catalog
        self._indexer_for, self._tagger, self._clock = indexer_for, tagger, clock
        self._new_id, self._redact = new_id, redact

    # —— 查询 ——

    def status(self) -> dict[str, Any]:
        """{"current": {"dataset_id","dataset_name"}, "rebuilds": [Rebuild.to_json() + "documents": 已写入版本数]}。"""
        return {"current": {"dataset_id": self._registry.dataset_id, "dataset_name": self._registry.dataset_name},
                "rebuilds": [{**r.to_json(), "documents": len(self._repo.docs(r.rebuild_id))} for r in self._repo.rebuilds()]}

    def _current(self) -> dict[str, tuple[list[dict[str, Any]], tuple[str, ...], str]]:
        """已发布版本 → (分块行, 标签, 签名)。"""
        result = {}
        for record in self._registry.documents():
            if record["status"] != "published":
                continue
            version = record["version_id"]
            detail = self._registry.detail(version)
            rows = detail["chunks"] if detail is not None else []
            tags = tuple(self._tagger.tags(version, record.get("folder", "")))
            result[version] = (rows, tags, chunk_signature(rows, tags))
        return result

    def _rebuild(self, rebuild_id: str, *statuses: str) -> Rebuild:
        rebuild = self._repo.get(rebuild_id)
        if rebuild is None:
            raise KeyError(rebuild_id)
        if statuses and rebuild.status not in statuses:
            raise ValueError(f"重建 {rebuild_id} 的状态是 {rebuild.status}，不能执行这一步")
        return rebuild

    # —— 开始 ——

    def start(self, dataset_id: str, *, by: str) -> Rebuild:
        """登记目标数据集并进入 building。
        - 已有 building → ValueError；dataset_id 是当前数据集 → ValueError；数据集不存在 → ValueError。
        - 该数据集已有 retired 记录（回退）→ 该记录改回 building，保留已写入的内容；其他已登记状态 → ValueError。
        - 新登记：数据集必须为空（document_count 为 0），否则 ValueError。"""
        if any(r.status == "building" for r in self._repo.rebuilds()):
            raise ValueError("已有正在进行的重建，请先完成或删除它")
        if dataset_id == self._registry.dataset_id:
            raise ValueError("目标数据集就是当前使用的数据集")
        info = self._catalog.info(dataset_id)
        if info is None:
            raise ValueError("检索引擎中没有这个数据集，请先在 RAGFlow 中创建（选好向量模型）")
        existing = next((r for r in self._repo.rebuilds() if r.dataset_id == dataset_id), None)
        if existing is not None:
            if existing.status != "retired":
                raise ValueError(f"该数据集已登记（状态 {existing.status}）")
            return self._repo.update(existing.rebuild_id, status="building", compare=None, compare_revision=None)
        if info.document_count != 0:
            raise ValueError("目标数据集必须是空的")
        rebuild = Rebuild(self._new_id(), dataset_id, info.name, info.embedding_model, "building", self._clock(), by)
        self._repo.create(rebuild)
        return rebuild

    # —— 同步 ——

    def sync(self, rebuild_id: str, *, limit: int | None = None,
             on_progress: Callable[[str, str], None] | None = None) -> SyncReport:
        """把目标数据集同步到登记处的当前状态（只限 building）：
        1. 计划：plan_sync(当前已发布版本的签名, 目标已写入的签名)；
        2. 移除：目标中多余的版本 indexer.remove 后 repo.drop_doc；
        3. 写入：按版本号顺序，至多 limit 个（None 为全部），indexer.publish(版本, 分块, 标签)，verify 的 indexed 为真后
           repo.put_doc；verify 未通过或引擎出错（TransientError / PermanentError）→ 记入 failed（错误经 redact），继续下一个。
        on_progress(动作, version_id) 在每个版本处理后调用，动作为 "published" / "removed" / "failed"。
        可以反复执行：签名相同的版本不再写入。"""
        if limit is not None and limit < 1:
            raise ValueError("limit 至少为 1")
        rebuild = self._rebuild(rebuild_id, "building")
        indexer = self._indexer_for(rebuild.dataset_id)
        current = self._current()
        built = self._repo.docs(rebuild_id)
        plan = plan_sync({v: c[2] for v, c in current.items()}, {v: d.signature for v, d in built.items()})
        report = SyncReport(unchanged=plan.unchanged)
        for version in plan.remove:
            try:
                indexer.remove(version)
            except (TransientError, PermanentError) as exc:
                report.failed.append({"version_id": version, "error": self._error(exc)})
                _notify(on_progress, "failed", version)
                continue
            self._repo.drop_doc(rebuild_id, version)
            report.removed.append(version)
            _notify(on_progress, "removed", version)
        todo = list(plan.publish) if limit is None else list(plan.publish[:limit])
        report.remaining = len(plan.publish) - len(todo)
        for version in todo:
            rows, tags, signature = current[version]
            try:
                if not rows:
                    raise PermanentError("登记处中没有分块")
                chunks = [Chunk(r["chunk_id"], r["text"], r["page_start"], r["page_end"], r["section_path"], r["meta"])
                          for r in rows]
                receipt = indexer.publish(version, chunks, list(tags))
                if not indexer.verify(version).indexed:
                    raise TransientError("写入后核对未通过")
            except (TransientError, PermanentError) as exc:
                report.failed.append({"version_id": version, "error": self._error(exc)})
                _notify(on_progress, "failed", version)
                continue
            self._repo.put_doc(rebuild_id, version, BuiltDoc(receipt.engine_doc_id, dict(receipt.chunk_map), signature))
            report.published.append(version)
            _notify(on_progress, "published", version)
        return report

    def _error(self, exc: Exception) -> str:
        return f"{type(exc).__name__}: {self._redact(str(exc))}"

    # —— 对比 ——

    def compare(self, rebuild_id: str, *, sample: int = 50, top_k: int = 6) -> dict[str, Any]:
        """抽样对比（只限 building）：从已发布且已写入目标的版本中按版本号等距取 sample 个，各取第一个 probe_query 非空的分块
        作为查询；用全部已发布版本标签的并集，分别在当前数据集与目标数据集中检索 top_k 条。
        结果为 compare_summary 加 {"sample","top_k","revision","at","details": [{"version_id","query","old","new"}…]}，
        并记入 repo（compare 与 compare_revision = 当时的 revision）。检索出错时原样抛出，不记录。"""
        if sample < 1 or top_k < 1:
            raise ValueError("sample 与 top_k 至少为 1")
        rebuild = self._rebuild(rebuild_id, "building")
        current = self._current()
        built = self._repo.docs(rebuild_id)
        versions = sample_evenly(sorted(v for v in current if v in built), sample)
        tags = sorted({tag for _, version_tags, _ in current.values() for tag in version_tags})
        old_index, new_index = self._indexer_for(self._registry.dataset_id), self._indexer_for(rebuild.dataset_id)
        probes = []
        for version in versions:
            query = next((q for q in (probe_query(str(r["text"])) for r in current[version][0]) if q), None)
            if query is None:
                continue
            old = tuple(h.version_id for h in old_index.search(query, group_tags=tags, top_k=top_k))
            new = tuple(h.version_id for h in new_index.search(query, group_tags=tags, top_k=top_k))
            probes.append(Probe(version, query, old, new))
        revision = self._repo.get(rebuild_id).revision
        result = {**compare_summary(probes), "sample": sample, "top_k": top_k, "revision": revision, "at": self._clock(),
                  "details": [{"version_id": p.version_id, "query": p.query, "old": list(p.old), "new": list(p.new)}
                              for p in probes]}
        self._repo.update(rebuild_id, compare=result, compare_revision=revision)
        return result

    # —— 切换 ——

    def switch(self, rebuild_id: str, *, by: str, accept_compare: bool = False) -> dict[str, Any]:
        """切换（只限 building，且应先停止 Web 与全部 worker）：
        1. 登记处已经在用目标数据集（上次切换在提交后中断）→ 只补记状态，返回；
        2. 同步必须已完成（plan_sync 为空），否则 ValueError；
        3. 必须有同步完成后（compare_revision == revision）的对比结果，且通过或 accept_compare，否则 ValueError；
        4. 旧数据集的记录：已有（switched / retired）→ replace_docs 为当前映射；没有 → 新建一条 retired（向量模型取自 catalog，
           查不到时为 "unknown"）。映射取自登记处当前的引擎 ID，签名为当前签名；
        5. registry.switch_index（提交点）；
        6. 旧记录改为 retired，目标改为 switched（switched_at、previous_dataset_id）。
        返回 {"rebuild_id","dataset_id","previous_dataset_id","documents"}。切换后必须重启 Web 与 worker。"""
        rebuild = self._rebuild(rebuild_id, "building", "switched")
        previous_dataset = self._registry.dataset_id
        if previous_dataset == rebuild.dataset_id:                 # 上次在提交点之后中断：只补记状态
            earlier = next((r.dataset_id for r in self._repo.rebuilds()
                            if r.status == "switched" and r.rebuild_id != rebuild_id), rebuild.previous_dataset_id)
            return self._finish_switch(rebuild, earlier)
        if rebuild.status != "building":
            raise ValueError("只能切换到正在重建的数据集")
        current = self._current()
        built = self._repo.docs(rebuild_id)
        plan = plan_sync({v: c[2] for v, c in current.items()}, {v: d.signature for v, d in built.items()})
        if not plan.done:
            raise ValueError(f"同步尚未完成：待写入 {len(plan.publish)} 个，待移除 {len(plan.remove)} 个，请先执行 sync")
        if rebuild.compare is None or rebuild.compare_revision != rebuild.revision:
            raise ValueError("请在同步完成后执行一次 compare")
        if not rebuild.compare.get("passed") and not accept_compare:
            raise ValueError("对比未通过；确认接受时加 accept_compare")
        previous_docs = {}
        for version, (rows, _, signature) in current.items():
            record = self._registry.detail(version)["document"]
            previous_docs[version] = BuiltDoc(record.get("engine_doc_id") or "",
                                              {r["chunk_id"]: r.get("engine_chunk_id") or "" for r in rows}, signature)
        previous = next((r for r in self._repo.rebuilds() if r.dataset_id == previous_dataset), None)
        if previous is None:
            info = self._catalog.info(previous_dataset)
            previous = Rebuild(self._new_id(), previous_dataset, self._registry.dataset_name,
                               info.embedding_model if info is not None else "unknown", "retired", self._clock(), by)
            self._repo.create(previous)
        self._repo.replace_docs(previous.rebuild_id, previous_docs)
        self._registry.switch_index(rebuild.dataset_id, rebuild.dataset_name,
                                    {v: (d.engine_doc_id, dict(d.chunk_map)) for v, d in built.items()})
        return self._finish_switch(rebuild, previous_dataset)

    def _finish_switch(self, rebuild: Rebuild, previous_dataset: str | None) -> dict[str, Any]:
        for other in self._repo.rebuilds():
            if other.status == "switched" and other.rebuild_id != rebuild.rebuild_id:
                self._repo.update(other.rebuild_id, status="retired")
            elif other.dataset_id == previous_dataset and other.status not in ("retired", "dropped"):
                self._repo.update(other.rebuild_id, status="retired")
        if rebuild.status != "switched":
            rebuild = self._repo.update(rebuild.rebuild_id, status="switched", switched_at=self._clock(),
                                        previous_dataset_id=previous_dataset)
        return {"rebuild_id": rebuild.rebuild_id, "dataset_id": rebuild.dataset_id,
                "previous_dataset_id": rebuild.previous_dataset_id, "documents": len(self._repo.docs(rebuild.rebuild_id))}

    # —— 删除 ——

    def drop(self, rebuild_id: str, *, confirm: str) -> None:
        """删除不再需要的数据集（状态 building 或 retired，且不是当前数据集）。confirm 必须等于其 dataset_id，否则 ValueError。
        catalog.delete 成功后记录改为 dropped、清空已写入记录。删除后不能再回退到它。"""
        rebuild = self._rebuild(rebuild_id, "building", "retired")
        if rebuild.dataset_id == self._registry.dataset_id:
            raise ValueError("不能删除当前使用的数据集")
        if confirm != rebuild.dataset_id:
            raise ValueError("确认值必须是要删除的数据集 ID")
        self._catalog.delete(rebuild.dataset_id)
        self._repo.replace_docs(rebuild_id, {})
        self._repo.update(rebuild_id, status="dropped")

    # —— 彻底删除文档时的清理 ——

    def forget(self, version_id: str) -> None:
        forget_everywhere(version_id, self._registry, self._repo, self._indexer_for)


def _notify(callback: Callable[[str, str], None] | None, action: str, version: str) -> None:
    if callback is not None:
        callback(action, version)


def forget_everywhere(version_id: str, registry: DocumentRegistry, repo: RebuildRepo,
                      indexer_for: Callable[[str], Indexer]) -> None:
    """批次 20b 的彻底删除同时调用：从所有 building / retired 数据集中移除该版本（不在当前数据集上操作），并删除其记录。
    引擎出错原样抛出（彻底删除可以重做）。"""
    for rebuild in repo.rebuilds():
        if rebuild.status in ("building", "retired") and rebuild.dataset_id != registry.dataset_id:
            indexer_for(rebuild.dataset_id).remove(version_id)
            if version_id in repo.docs(rebuild.rebuild_id):
                repo.drop_doc(rebuild.rebuild_id, version_id)
