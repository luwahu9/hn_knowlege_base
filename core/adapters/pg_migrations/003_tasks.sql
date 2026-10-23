-- 批次 15c：任务队列与跨进程版本号分配。规格见 docs/spec-26-task-queue.md。

CREATE TABLE tasks (
    seq             bigserial NOT NULL UNIQUE,            -- 入队顺序
    task_id         text COLLATE "C" PRIMARY KEY,
    idempotency_key text COLLATE "C" NOT NULL UNIQUE,
    kind            text COLLATE "C" NOT NULL,
    payload         jsonb NOT NULL,
    attempt         integer NOT NULL CHECK (attempt >= 1),
    status          text NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed')),
    worker_id       text,
    last_error      text,
    progress        text,
    lease_until     timestamptz,
    enqueued_at     timestamptz NOT NULL,
    finished_at     timestamptz,
    CHECK ((status = 'running') = (worker_id IS NOT NULL AND lease_until IS NOT NULL))
);
-- 领取：按种类、入队顺序找排队中或租约到期的任务。
CREATE INDEX tasks_claimable ON tasks (kind, seq) WHERE status IN ('queued', 'running');
CREATE INDEX tasks_kind_status ON tasks (kind, status, seq);

-- 版本号：v{序号}_{内容哈希前 10 位} 中的序号，跨进程只增不减、不复用。
CREATE SEQUENCE version_number_seq;
SELECT setval('version_number_seq',
              GREATEST(1, COALESCE((SELECT max(substring(version_id from '^v([0-9]+)_')::bigint) FROM documents), 0)),
              (SELECT count(*) > 0 FROM documents));
