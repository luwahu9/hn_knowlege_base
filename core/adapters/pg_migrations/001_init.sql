-- 批次 15a：文档登记底座。规格见 docs/spec-24-registry.md。
-- 每个迁移在一个事务中执行；已执行的版本记录在 schema_migrations，不得修改已发布的迁移文件，只能追加新文件。

CREATE TABLE kb_meta (
    key   text PRIMARY KEY,
    value text NOT NULL
);

-- 文件夹树：path 为规范路径（core/domain/folders.py），根为 ''，由本迁移插入。
CREATE TABLE folders (
    id         bigserial PRIMARY KEY,
    path       text NOT NULL UNIQUE,
    parent_id  bigint REFERENCES folders(id),
    name       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((path = '') = (parent_id IS NULL))
);
INSERT INTO folders (path, parent_id, name) VALUES ('', NULL, '');

CREATE TABLE documents (
    seq          bigserial PRIMARY KEY,
    version_id   text NOT NULL UNIQUE,
    folder_id    bigint NOT NULL REFERENCES folders(id),
    status       text NOT NULL,
    title        text NOT NULL,
    content_hash text,
    imported_at  text,
    record       jsonb NOT NULL
);
CREATE INDEX documents_folder ON documents (folder_id);
CREATE INDEX documents_published_hash ON documents (content_hash, seq) WHERE status = 'published';

CREATE TABLE chunks (
    version_id text NOT NULL REFERENCES documents(version_id),
    ordinal    integer NOT NULL,
    chunk_id   text NOT NULL UNIQUE,
    row        jsonb NOT NULL,
    PRIMARY KEY (version_id, ordinal)
);

CREATE TABLE unanswered (
    id       bigserial PRIMARY KEY,
    time     text NOT NULL,
    question text NOT NULL,
    status   text NOT NULL
);

-- 知识事件（发件箱）。seq 由序列生成，可能有空洞（事务回滚），订阅者只依赖单调递增。
CREATE TABLE knowledge_events (
    seq        bigserial PRIMARY KEY,
    kind       text NOT NULL CHECK (kind IN ('document_published', 'document_removed', 'extraction_done',
                                             'links_done', 'decision_changed')),
    version_id text,
    payload    jsonb NOT NULL DEFAULT '{}'::jsonb,
    time       text NOT NULL
);

CREATE TABLE event_offsets (
    subscriber text PRIMARY KEY,
    seq        bigint NOT NULL CHECK (seq >= 0)
);

-- 预留：LLM Wiki（批次 24）。本批次只建表，不读写。
CREATE TABLE wiki_pages (
    id         bigserial PRIMARY KEY,
    slug       text NOT NULL UNIQUE,
    title      text NOT NULL,
    folder_id  bigint NOT NULL REFERENCES folders(id),
    kind       text NOT NULL,
    status     text NOT NULL DEFAULT 'draft',
    current_revision bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE wiki_revisions (
    id          bigserial PRIMARY KEY,
    page_id     bigint NOT NULL REFERENCES wiki_pages(id),
    number      integer NOT NULL,
    body        text NOT NULL,
    author      text NOT NULL,
    source_seq  bigint,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (page_id, number)
);

CREATE TABLE wiki_claims (
    id          bigserial PRIMARY KEY,
    revision_id bigint NOT NULL REFERENCES wiki_revisions(id),
    ordinal     integer NOT NULL,
    text        text NOT NULL,
    citations   jsonb NOT NULL DEFAULT '[]'::jsonb,
    UNIQUE (revision_id, ordinal)
);
