-- 批次 21b：检索数据集重建（docs/spec-39-reindex.md）。
CREATE TABLE index_rebuilds (
    seq                 bigserial,
    rebuild_id          text COLLATE "C" PRIMARY KEY,
    dataset_id          text NOT NULL UNIQUE,
    dataset_name        text NOT NULL,
    embedding_model     text NOT NULL,
    status              text NOT NULL CHECK (status IN ('building', 'switched', 'retired', 'dropped')),
    created_at          text NOT NULL,
    created_by          text NOT NULL,
    revision            bigint NOT NULL DEFAULT 0,
    compare             jsonb,
    compare_revision    bigint,
    switched_at         text,
    previous_dataset_id text
);
CREATE UNIQUE INDEX index_rebuilds_one_building ON index_rebuilds ((true)) WHERE status = 'building';

CREATE TABLE index_rebuild_docs (
    rebuild_id    text COLLATE "C" NOT NULL REFERENCES index_rebuilds(rebuild_id),
    version_id    text NOT NULL,
    engine_doc_id text NOT NULL,
    chunk_map     jsonb NOT NULL,
    signature     text NOT NULL,
    PRIMARY KEY (rebuild_id, version_id)
);
