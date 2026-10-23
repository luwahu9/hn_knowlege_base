"""把 JSON 数据目录迁入 PostgreSQL。规格见 docs/spec-24-registry.md 第四节、docs/spec-25-knowledge-db.md 第七节。

用法（凭据只从环境读取）：
    HAINA_PG_DSN=postgresql://haina:***@127.0.0.1:5432/haina python -m tools.pg.migrate_kb --data kb_data_glm [--schema public] [--dry-run]

- 文档记录、分块、文件夹、未收录问题（migrate_kb），以及批次 15b 起的实体、抽取结果、引用、冲突判断、审核决定
  （migrate_knowledge）。源目录只读，原件（blobs/）不迁移。
- 不迁移知识事件，迁入的文档也不产生事件（它们已经抽取过）；Web 启动后的 enqueue_missing 照常补漏。
- 可重复运行：目标中已有且内容相同的版本跳过；已有但内容不同 → 报告冲突并以 1 退出，不覆盖。
- 结束后逐版本比对文档记录与分块，报告写到标准输出（JSON）。源目录只读。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from apps.web.store import KbStore
from core.adapters.pg_registry import PgRegistry, initialize, migrate


def migrate_kb(source: KbStore, dsn: str, *, schema: str = "public", dry_run: bool = False) -> dict[str, Any]:
    """返回 {"applied_migrations","documents","imported","skipped","conflicts","folders","unanswered","verified"}。
    dry_run 时只读取目标（不迁移结构、不写入），目标未迁移时视为空库。"""
    from core.adapters.pg_registry import _connect

    records = source.documents()
    details = {row["version_id"]: source.detail(row["version_id"]) for row in records}
    report: dict[str, Any] = {"applied_migrations": [], "documents": len(records), "imported": [],
                              "skipped": [], "conflicts": [], "folders": len(source.folders()),
                              "unanswered": "skipped", "verified": False}
    target: PgRegistry | None = None
    try:
        if dry_run:
            with _connect(dsn, schema, autocommit=True) as conn:
                exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name='kb_meta'", (schema,)).fetchone()
            if exists:
                target = PgRegistry(dsn, schema=schema)
                if (target.dataset_id, target.dataset_name, target.group_tag) != (source.dataset_id, source.dataset_name, source.group_tag):
                    raise ValueError("不得覆盖已有知识库元数据")
        else:
            report["applied_migrations"] = migrate(dsn, schema=schema)
            initialize(dsn, schema=schema, dataset_id=source.dataset_id, dataset_name=source.dataset_name, group_tag=source.group_tag)
            target = PgRegistry(dsn, schema=schema)

        for row in records:
            version = row["version_id"]
            current = target.detail(version) if target is not None else None
            if current is None:
                report["imported"].append(version)
            elif current == details[version]:
                report["skipped"].append(version)
            else:
                report["conflicts"].append(version)
        if report["conflicts"]:
            report["imported"] = []
            return report
        if not dry_run:
            for version in report["imported"]:
                detail = details[version]
                target.import_document(detail["document"], detail["chunks"])
            for path in source.folders():
                target.ensure_folder(path)
        questions = source.unanswered()
        existing = target.unanswered() if target is not None else []
        if existing == questions:
            report["unanswered"] = "same"
        elif existing:
            report["unanswered"] = "conflict"
        else:
            report["unanswered"] = "imported"
            if not dry_run:
                target.import_unanswered(questions)
        report["verified"] = False
        if target is not None and not dry_run and report["unanswered"] != "conflict":
            # 架构方补充（验收后）：用新连接从数据库重新读取后比对，不依赖写入时的进程内缓存。
            check = PgRegistry(dsn, schema=schema)
            try:
                report["verified"] = (all(check.detail(version) == detail for version, detail in details.items())
                                      and set(source.folders()) <= set(check.folders())
                                      and check.unanswered() == questions)
            finally:
                check.close()
        return report
    finally:
        if target is not None:
            target.close()


def _snapshot(knowledge: Any, links: Any, decisions: Any) -> dict[str, Any]:
    return {"entities": {row.id: row.to_json() for row in knowledge.entities()},
            "documents": {version: knowledge.document(version) for version in sorted(knowledge.extracted())},
            "references": {version: links.document(version) for version in sorted(links.extracted())},
            "judgements": {row.pair_id: row.to_json() for row in links.conflicts()},
            "decisions": {"next": decisions.next_number() if hasattr(decisions, "next_number") else decisions._next,
                          "decisions": [row.to_json() for row in decisions.decisions()]}}


def migrate_knowledge(data_root: str | Path, dsn: str, *, schema: str = "public",
                      dry_run: bool = False) -> dict[str, Any]:
    """迁移 data_root 下的知识数据。目标须已由 migrate_kb 迁移结构并写入元数据（dry_run 时目标未迁移视为空）。

    返回 {"entities": {"imported","skipped","conflicts"}, "documents": {...}, "references": {...},
          "judgements": {...}, "decisions": "imported"|"same"|"conflict"|"empty", "verified": bool}，
    其中各 {...} 的值为 ID 列表。
    实体：目标必须是源列表的前缀，逐位置比较 ID 与内容；相同前缀跳过，之后的源实体按顺序追加。
    不同位置依次记录目标、源的 ID（源不足时只记目标），去重保留首次出现顺序，作为 conflicts。
    抽取结果、引用提取结果按版本、冲突判断按 pair_id 比较，相同跳过、不同记冲突。
    审核决定的初始状态为列表为空且下一个编号为 1：双方初始为 empty；只有目标初始为 imported；
    非初始目标与源的列表及下一个编号都相同为 same；其余为 conflict，绝不调小用过的编号。
    任何一类有冲突时不写入任何数据。完成后重算叠加表，用新连接回读，与源逐项比对得出 verified。"""
    from contextlib import ExitStack
    from apps.web.knowledge import KnowledgeStore
    from apps.web.links import LinkStore
    from apps.web.review import DecisionStore
    from core.adapters.pg_knowledge import _DECISION_LOCK, open_knowledge
    from core.adapters.pg_registry import _connect
    from core.domain.knowledge import EntityRecord
    from core.domain.relations import ConflictRecord
    from core.domain.review import Decision

    source = _snapshot(KnowledgeStore(data_root), LinkStore(data_root), DecisionStore(data_root))
    kinds = ("entities", "documents", "references", "judgements")
    report: dict[str, Any] = {kind: {"imported": [], "skipped": [], "conflicts": []} for kind in kinds}
    report.update(decisions="empty", verified=False)
    db = None
    try:
        with ExitStack() as guard:
            if dry_run:
                with _connect(dsn, schema, autocommit=True) as conn:
                    exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name='entities'", (schema,)).fetchone()
            else:
                exists = True
            if exists:
                db, knowledge, links, decisions, _ = open_knowledge(dsn, schema=schema)
                conn = guard.enter_context(db._read() if dry_run else db.tx())
                if not dry_run:
                    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DECISION_LOCK,))
                target = _snapshot(knowledge, links, decisions)
            else:
                target = {**{kind: {} for kind in kinds}, "decisions": {"next": 1, "decisions": []}}
            source_entities = list(source["entities"].items())
            target_entities = list(target["entities"].items())
            for index, existing in enumerate(target_entities):
                if index < len(source_entities) and existing == source_entities[index]:
                    report["entities"]["skipped"].append(existing[0])
                else:
                    report["entities"]["conflicts"].append(existing[0])
                    if index < len(source_entities):
                        report["entities"]["conflicts"].append(source_entities[index][0])
            report["entities"]["conflicts"] = list(dict.fromkeys(report["entities"]["conflicts"]))
            if not report["entities"]["conflicts"]:
                report["entities"]["imported"] = [id_ for id_, _ in source_entities[len(target_entities):]]
            for kind in kinds[1:]:
                for id_, value in source[kind].items():
                    category = ("imported" if id_ not in target[kind] else
                                "skipped" if target[kind][id_] == value else "conflicts")
                    report[kind][category].append(id_)
            initial = {"next": 1, "decisions": []}
            if target["decisions"] == initial:
                report["decisions"] = "empty" if source["decisions"] == initial else "imported"
            elif source["decisions"] == target["decisions"]:
                report["decisions"] = "same"
            else:
                report["decisions"] = "conflict"
            if any(report[kind]["conflicts"] for kind in kinds) or report["decisions"] == "conflict":
                for kind in kinds:
                    report[kind]["imported"] = []
                return report
            if dry_run:
                return report
            with db.tx() as conn:
                knowledge.import_entities([EntityRecord.from_json(source["entities"][id_]) for id_ in report["entities"]["imported"]])
                for version in report["documents"]["imported"]:
                    knowledge.import_document(source["documents"][version])
                for version in report["references"]["imported"]:
                    links.import_document(source["references"][version])
                for id_ in report["judgements"]["imported"]:
                    links.add_conflict(ConflictRecord.from_json(source["judgements"][id_]))
                if report["decisions"] == "imported":
                    decisions.import_decisions([Decision.from_json(row) for row in source["decisions"]["decisions"]],
                                               source["decisions"]["next"])
                db.refresh_overlay(conn)
        check, check_knowledge, check_links, check_decisions, _ = open_knowledge(dsn, schema=schema)
        try:
            actual = _snapshot(check_knowledge, check_links, check_decisions)
            report["verified"] = (list(actual["entities"].items()) == source_entities
                                  and all(actual[kind].get(id_) == value for kind in kinds[1:] for id_, value in source[kind].items())
                                  and actual["decisions"] == source["decisions"])
        finally:
            check.close()
        return report
    finally:
        if db is not None:
            db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.pg.migrate_kb", description="JSON 数据目录迁入 PostgreSQL")
    parser.add_argument("--data", required=True, help="已初始化的 Web 数据目录（含 kb.json）")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    dsn = os.environ.get("HAINA_PG_DSN")
    if not dsn:
        print("缺少环境变量：HAINA_PG_DSN")
        return 2
    try:
        report = migrate_kb(KbStore(args.data), dsn, schema=args.schema, dry_run=args.dry_run)
        ok = not report["conflicts"] and report["unanswered"] != "conflict" and (args.dry_run or report["verified"])
        knowledge = migrate_knowledge(args.data, dsn, schema=args.schema, dry_run=args.dry_run) if ok else None
    except ValueError as exc:
        print(str(exc).replace(dsn, "***"))
        return 2
    report = {**report, "imported": len(report["imported"]), "skipped": len(report["skipped"])}
    if knowledge is not None:
        report["knowledge"] = {key: ({k: (len(v) if k != "conflicts" else v) for k, v in value.items()}
                                     if isinstance(value, dict) else value) for key, value in knowledge.items()}
        ok = (not any(knowledge[kind]["conflicts"] for kind in ("entities", "documents", "references", "judgements"))
              and knowledge["decisions"] != "conflict" and (args.dry_run or knowledge["verified"]))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
