-- 批次 15b：实体、事件、提及、引用、冲突判断、审核决定迁入数据库。规格见 docs/spec-25-knowledge-db.md。

-- 文档记录的修订号：插入或更新时取新值，供多进程缓存增量刷新。
CREATE SEQUENCE documents_rev_seq;
ALTER TABLE documents ADD COLUMN rev bigint;
UPDATE documents SET rev = nextval('documents_rev_seq');
ALTER TABLE documents ALTER COLUMN rev SET NOT NULL;
ALTER TABLE documents ALTER COLUMN rev SET DEFAULT nextval('documents_rev_seq');
CREATE UNIQUE INDEX documents_rev ON documents (rev);

-- 所有标识符按码点排序（与 Python 的字符串比较一致），不受数据库默认排序规则影响。
ALTER TABLE documents ALTER COLUMN version_id TYPE text COLLATE "C";
ALTER TABLE chunks ALTER COLUMN version_id TYPE text COLLATE "C";
ALTER TABLE chunks ALTER COLUMN chunk_id TYPE text COLLATE "C";
ALTER TABLE knowledge_events ALTER COLUMN version_id TYPE text COLLATE "C";

-- 原始抽取结果（不引用 documents：与 JSON 实现一样，知识数据可先于或独立于文档登记写入）。data 为 to_json() 的完整内容，其余列只为查询与索引而冗余存放，写入时由同一记录导出。
CREATE TABLE entities (
    ordinal           bigserial NOT NULL UNIQUE,      -- 创建顺序
    id                text COLLATE "C" PRIMARY KEY,
    type              text NOT NULL,
    has_candidates    boolean NOT NULL,
    data              jsonb NOT NULL
);
CREATE INDEX entities_review ON entities (id) WHERE has_candidates;

CREATE TABLE knowledge_docs (
    version_id   text COLLATE "C" PRIMARY KEY,
    extracted_at text NOT NULL,
    report       jsonb NOT NULL
);

CREATE TABLE events (
    event_id        text COLLATE "C" PRIMARY KEY,
    version_id      text COLLATE "C" NOT NULL REFERENCES knowledge_docs(version_id) ON DELETE CASCADE,
    ordinal         integer NOT NULL,                 -- 在本版本抽取结果中的顺序
    verdict         text NOT NULL,
    duplicate       boolean NOT NULL,
    subject_entity  text COLLATE "C",
    object_entity   text COLLATE "C",
    date_start      date,
    date_end        date,
    action_key      text COLLATE "C" NOT NULL,        -- relations.action_key(action)
    fingerprint     text COLLATE "C",                 -- 空串存为 NULL
    data            jsonb NOT NULL,
    -- 有效值（叠加审核决定之后），由 refresh_overlay 维护；插入时等于原始值。
    eff_verdict     text NOT NULL,
    eff_subject     text COLLATE "C",
    eff_object      text COLLATE "C"
);
CREATE INDEX events_version ON events (version_id);
-- 原始参与者：叠加表重算时定位受影响的行。
CREATE INDEX events_raw_subject ON events (subject_entity) WHERE subject_entity IS NOT NULL;
CREATE INDEX events_raw_object ON events (object_entity) WHERE object_entity IS NOT NULL;
-- 有效参与者：实体详情（按时间倒序取最近的事件）、局部图。
-- INCLUDE 的列让局部图与实体详情可以只扫索引、不读宽行（data 列较大）。
CREATE INDEX events_subject ON events (eff_subject, date_start DESC, date_end DESC, event_id DESC)
    INCLUDE (version_id, eff_verdict, eff_object) WHERE eff_subject IS NOT NULL AND NOT duplicate;
CREATE INDEX events_object ON events (eff_object, date_start DESC, date_end DESC, event_id DESC)
    INCLUDE (version_id, eff_verdict, eff_subject) WHERE eff_object IS NOT NULL AND NOT duplicate;
CREATE INDEX events_dated ON events (date_start, date_end, event_id) WHERE date_start IS NOT NULL AND NOT duplicate;
CREATE INDEX events_undated ON events (event_id) WHERE date_start IS NULL AND NOT duplicate;
CREATE INDEX events_review ON events (event_id) WHERE eff_verdict = 'review' AND NOT duplicate;
CREATE INDEX events_pairs ON events (eff_subject, action_key, event_id)
    WHERE eff_verdict = 'pass' AND NOT duplicate AND eff_subject IS NOT NULL AND action_key <> '';

CREATE TABLE mentions (
    version_id  text COLLATE "C" NOT NULL REFERENCES knowledge_docs(version_id) ON DELETE CASCADE,
    ordinal     integer NOT NULL,
    entity_id   text COLLATE "C" NOT NULL,
    data        jsonb NOT NULL,
    PRIMARY KEY (version_id, ordinal)
);
CREATE INDEX mentions_entity ON mentions (entity_id, version_id);

CREATE TABLE reference_docs (
    version_id   text COLLATE "C" PRIMARY KEY,
    extracted_at text NOT NULL,
    report       jsonb NOT NULL
);

CREATE TABLE doc_references (
    ref_id      text COLLATE "C" PRIMARY KEY,
    version_id  text COLLATE "C" NOT NULL REFERENCES reference_docs(version_id) ON DELETE CASCADE,
    ordinal     integer NOT NULL,
    data        jsonb NOT NULL
);
CREATE INDEX doc_references_version ON doc_references (version_id);

CREATE TABLE conflicts (
    pair_id    text COLLATE "C" PRIMARY KEY,
    event_a    text COLLATE "C" NOT NULL,
    event_b    text COLLATE "C" NOT NULL,
    version_a  text COLLATE "C" NOT NULL,
    version_b  text COLLATE "C" NOT NULL,
    verdict    text NOT NULL,
    data       jsonb NOT NULL
);
CREATE INDEX conflicts_open ON conflicts (pair_id) WHERE verdict IN ('conflict', 'review');

-- 审核决定。同一 (grp, target) 只保留最新一条（与 JSON 实现相同）；编号只增不减。
CREATE TABLE decisions (
    number   integer PRIMARY KEY CHECK (number >= 1),
    grp      text COLLATE "C" NOT NULL,
    target   text COLLATE "C" NOT NULL,
    kind     text NOT NULL,
    data     jsonb NOT NULL,
    UNIQUE (grp, target)
);
INSERT INTO kb_meta (key, value) VALUES ('next_decision', '1') ON CONFLICT (key) DO NOTHING;

-- 审核决定的叠加结果（物化）：每次决定变化或抽取结果保存时，在同一事务中由纯函数重新计算并整体替换。
CREATE TABLE entity_redirects (
    source  text COLLATE "C" PRIMARY KEY,
    target  text COLLATE "C" NOT NULL
);
CREATE INDEX entity_redirects_target ON entity_redirects (target);

CREATE TABLE event_overrides (
    event_id  text COLLATE "C" PRIMARY KEY,
    verdict   text NOT NULL
);

-- 新的知识事件类型（core/app/registry.py 的 CONFLICTS_JUDGED）。
ALTER TABLE knowledge_events DROP CONSTRAINT knowledge_events_kind_check;
ALTER TABLE knowledge_events ADD CONSTRAINT knowledge_events_kind_check CHECK (kind IN (
    'document_published', 'document_removed', 'extraction_done', 'links_done', 'decision_changed', 'conflicts_judged'));

-- 有效事件：叠加实体合并与事件审核后的视图（与 core/domain/review.effective_events 一致）。
-- 其中的有效列由 refresh_overlay 物化在 events 上（eff_*），必须始终满足：
--   eff_verdict = COALESCE(event_overrides.verdict, verdict)
--   eff_subject = COALESCE(entity_redirects.target, subject_entity)，eff_object 同理。
CREATE VIEW effective_events AS
SELECT e.event_id, e.version_id, e.ordinal, e.eff_verdict AS verdict, e.duplicate,
       e.eff_subject AS subject_entity, e.eff_object AS object_entity,
       e.subject_entity AS raw_subject, e.object_entity AS raw_object,
       e.date_start, e.date_end, e.action_key, e.fingerprint, e.data
FROM events e;

-- 问答命中回填：按 (版本, 引擎分块 ID) 直接查分块。
CREATE INDEX chunks_engine ON chunks (version_id, (row->>'engine_chunk_id'));
CREATE INDEX documents_published_title ON documents (version_id) INCLUDE (title) WHERE status = 'published';

-- 文档记录被更新（如 15c 的审核、批次 20 的删除）时取新的修订号。
CREATE FUNCTION documents_bump_rev() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.rev := nextval('documents_rev_seq');
    RETURN NEW;
END $$;
CREATE TRIGGER documents_rev_on_update BEFORE UPDATE ON documents FOR EACH ROW EXECUTE FUNCTION documents_bump_rev();
