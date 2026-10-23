"""批次 21b：检索数据集重建（纯逻辑）。规格见 docs/spec-39-reindex.md。

- 签名：一个已发布版本在检索引擎中的内容 = 分块（chunk_id 与正文，按顺序）+ 用户组标签。签名相同 → 不需要重新写入。
- 同步计划：当前应有的内容（已发布版本 → 签名）与目标数据集已有的内容比较，得出要写入与要移除的版本。
- 对比：抽样分块，用其开头作为查询，分别在旧、新数据集中检索，统计“能找回原文档”的比例与结果重合度。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

QUERY_CHARS = 60              # 抽样查询取分块正文开头的字数
MIN_QUERY_CHARS = 8           # 正文去空白后不足这么多字的分块不用作查询
COMPARE_TOLERANCE = 0.05      # 新数据集的找回率不低于旧数据集减去它，才算对比通过
REBUILD_STATUSES = ("building", "switched", "retired", "dropped")
_SPACE = re.compile(r"\s+")


def chunk_signature(chunks: Sequence[Mapping[str, object]], tags: Iterable[str]) -> str:
    """sha256（十六进制）：对 {"chunks": [[chunk_id, text]…按给定顺序], "tags": 去重排序后的标签} 的紧凑 JSON
    （ensure_ascii=False、键排序、无空白）求哈希。页码、章节等不进检索引擎的字段不参与。"""
    payload = {"chunks": [[str(c["chunk_id"]), str(c["text"])] for c in chunks], "tags": sorted(set(tags))}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SyncPlan:
    publish: tuple[str, ...]       # 目标中没有、或签名不同的已发布版本（按版本号升序）
    remove: tuple[str, ...]        # 目标中有、但已不是已发布的版本（按版本号升序）
    unchanged: int

    @property
    def done(self) -> bool:
        return not self.publish and not self.remove


def plan_sync(current: Mapping[str, str], built: Mapping[str, str]) -> SyncPlan:
    """current：已发布版本 → 应有签名；built：目标数据集已写入的版本 → 写入时的签名。"""
    publish = tuple(sorted(v for v, signature in current.items() if built.get(v) != signature))
    remove = tuple(sorted(v for v in built if v not in current))
    return SyncPlan(publish, remove, len(current) - len(publish))


def sample_evenly(items: Sequence[str], n: int) -> list[str]:
    """从有序序列中等距取至多 n 项（确定性）：n ≥ len → 全部；n ≤ 0 → 空；否则取下标 floor(i·len/n)，i = 0..n-1。"""
    if n <= 0:
        return []
    if n >= len(items):
        return list(items)
    return [items[i * len(items) // n] for i in range(n)]


def probe_query(text: str) -> str | None:
    """抽样查询：去掉面包屑行（以“【”开头、“】”结尾的第一行），空白折叠为一个空格并去首尾空白，取前 QUERY_CHARS 字；
    不足 MIN_QUERY_CHARS 字 → None。"""
    first, _, rest = text.partition("\n")
    body = rest if first.startswith("【") and first.endswith("】") else text
    body = _SPACE.sub(" ", body).strip()
    return body[:QUERY_CHARS] if len(body) >= MIN_QUERY_CHARS else None


@dataclass(frozen=True)
class Probe:
    version_id: str                # 查询取自哪个版本（期望找回它）
    query: str
    old: tuple[str, ...]           # 旧数据集返回的版本号（按名次，可重复）
    new: tuple[str, ...]


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    x, y = set(a), set(b)
    return 1.0 if not x and not y else len(x & y) / len(x | y)


def compare_summary(probes: Sequence[Probe], *, tolerance: float = COMPARE_TOLERANCE) -> dict[str, object]:
    """{"probes": 条数, "old_hit_rate", "new_hit_rate"（结果中含 version_id 的比例）, "overlap"（两边版本集合的 Jaccard 平均）,
    "new_misses": [新数据集没找回、旧数据集找回的 version_id…按出现顺序], "passed": new ≥ old − tolerance}。
    比例保留 4 位小数；没有抽样时两个比例与 overlap 都为 0、passed 为 False。"""
    if not probes:
        return {"probes": 0, "old_hit_rate": 0.0, "new_hit_rate": 0.0, "overlap": 0.0, "new_misses": [], "passed": False}
    old = sum(p.version_id in p.old for p in probes) / len(probes)
    new = sum(p.version_id in p.new for p in probes) / len(probes)
    overlap = sum(_jaccard(p.old, p.new) for p in probes) / len(probes)
    misses = [p.version_id for p in probes if p.version_id in p.old and p.version_id not in p.new]
    return {"probes": len(probes), "old_hit_rate": round(old, 4), "new_hit_rate": round(new, 4),
            "overlap": round(overlap, 4), "new_misses": misses, "passed": new >= old - tolerance - 1e-9}
