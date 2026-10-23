"""检索数据集重建与切换（批次 21b）。规格：docs/spec-39-reindex.md。只支持数据库模式（HAINA_PG_DSN）。

用法（在仓库根目录，环境变量与 Web 相同）：
    python -m tools.reindex status
    python -m tools.reindex start   --dataset-id <新数据集 ID> [--by <操作人>]
    python -m tools.reindex sync    <重建 ID> [--limit N]
    python -m tools.reindex compare <重建 ID> [--sample 50] [--top-k 6] [--out <文件>]
    python -m tools.reindex switch  <重建 ID> --confirm <重建 ID> [--accept-compare]
    python -m tools.reindex drop    <重建 ID> --confirm <数据集 ID>
公共参数：--data <原件目录，缺省 kb_data>、--open（与 Web 的 --open 对应：标签用固定的分组标签）。

新数据集先在 RAGFlow 中创建并选好向量模型（保持为空），再用 start 登记。sync 可反复执行；switch 前须停止 Web 与全部 worker，
切换后用新配置重启。回退：对 status 中 retired 的旧数据集再走一次 start → sync → compare → switch。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from apps.web.assembly import location_problems, missing_environment, open_stores, redactor_from_environment
from apps.web.jobs import china_now
from apps.web.store import now_iso
from core.adapters.ragflow_http import RagflowDatasets, RagflowIndexer
from core.app.permissions import LegacyTagger, PermissionService, PermissionTagger
from core.app.ports import PermanentError, TransientError
from core.app.reindex import ReindexService


ACTIONS = {"published": "写入", "removed": "移除", "failed": "失败"}


def build_service(args: argparse.Namespace, http: httpx.Client, redact: Any) -> ReindexService:
    parts = open_stores(Path(args.data).resolve())
    if "rebuilds" not in parts:
        raise ValueError("重建只支持数据库模式，请设置 HAINA_PG_DSN")
    store = parts["store"]
    if parts.get("permission_repo") is not None and not args.open:
        tagger: Any = PermissionTagger(PermissionService(store, parts["permission_repo"], known_groups=lambda: (),
                                                         clock=china_now))
    else:
        tagger = LegacyTagger(store.group_tag)
    url, key = os.environ["RAGFLOW_URL"], os.environ["RAGFLOW_API_KEY"]

    def indexer_for(dataset_id: str) -> RagflowIndexer:
        return RagflowIndexer(url, key, dataset_id, timeout_seconds=120, http=http)

    return ReindexService(store, parts["rebuilds"], RagflowDatasets(url, key, http=http), indexer_for, tagger, now_iso,
                          redact=redact)


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def run(args: argparse.Namespace, service: ReindexService) -> int:
    if args.command == "status":
        _print(service.status())
    elif args.command == "start":
        _print(service.start(args.dataset_id, by=args.by).to_json())
    elif args.command == "sync":
        def progress(action: str, version: str) -> None:
            print(f"{ACTIONS[action]} {version}", flush=True)
        report = service.sync(args.rebuild_id, limit=args.limit, on_progress=progress)
        _print(report.to_json())
        return 1 if report.failed else 0
    elif args.command == "compare":
        result = service.compare(args.rebuild_id, sample=args.sample, top_k=args.top_k)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _print({key: value for key, value in result.items() if key != "details"})
        return 0 if result["passed"] else 1
    elif args.command == "switch":
        if args.confirm != args.rebuild_id:
            print("--confirm 必须与重建 ID 相同")
            return 2
        print("提示：切换前应已停止 Web 与全部 worker；切换后请重启它们。", flush=True)
        _print(service.switch(args.rebuild_id, by=args.by, accept_compare=args.accept_compare))
    elif args.command == "drop":
        service.drop(args.rebuild_id, confirm=args.confirm)
        print("已删除")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.reindex", description="海纳知识库检索数据集重建与切换")
    parser.add_argument("--data", default="kb_data")
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--by", default="reindex-cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    start = sub.add_parser("start")
    start.add_argument("--dataset-id", required=True)
    sync = sub.add_parser("sync")
    sync.add_argument("rebuild_id")
    sync.add_argument("--limit", type=int)
    compare = sub.add_parser("compare")
    compare.add_argument("rebuild_id")
    compare.add_argument("--sample", type=int, default=50)
    compare.add_argument("--top-k", type=int, default=6)
    compare.add_argument("--out")
    switch = sub.add_parser("switch")
    switch.add_argument("rebuild_id")
    switch.add_argument("--confirm", required=True)
    switch.add_argument("--accept-compare", action="store_true")
    drop = sub.add_parser("drop")
    drop.add_argument("rebuild_id")
    drop.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    missing = missing_environment()
    if missing:
        print("缺少环境变量：" + ", ".join(missing))
        return 2
    problems, _ = location_problems()
    if problems:
        print("\n".join(problems))
        return 2
    _, redactor = redactor_from_environment()
    try:
        with httpx.Client() as http:
            return run(args, build_service(args, http, redactor.text))
    except (ValueError, KeyError) as exc:
        print(redactor.error(exc) if not isinstance(exc, KeyError) else f"没有这个重建：{exc.args[0]}")
        return 2
    except (TransientError, PermanentError) as exc:
        print(redactor.error(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
