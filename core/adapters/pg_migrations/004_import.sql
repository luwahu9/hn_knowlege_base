-- 批次 15d：文件夹导入与目录扫描。规格见 docs/spec-27-folder-import.md。

-- 按原件哈希前缀查已登记版本（find_by_digest），以及取最大版本序号（next_version_number）。
CREATE INDEX documents_digest ON documents (right(version_id, 10));
CREATE INDEX documents_version_number ON documents ((substring(version_id from '^v([0-9]+)_')::bigint));

-- 扫描索引：每个扫描根下每个文件的最近状态。
CREATE TABLE scan_files (
    root       text COLLATE "C" NOT NULL,
    path       text COLLATE "C" NOT NULL,
    size       bigint NOT NULL CHECK (size >= 0),
    mtime_ns   bigint NOT NULL,
    sha256     text COLLATE "C" NOT NULL,
    version_id text COLLATE "C",
    outcome    text NOT NULL CHECK (outcome IN ('new', 'changed', 'unchanged', 'duplicate', 'rejected')),
    PRIMARY KEY (root, path)
);

-- 扫描报告。
CREATE TABLE scan_runs (
    scan_id    text COLLATE "C" PRIMARY KEY,
    root       text COLLATE "C" NOT NULL,
    started_at text NOT NULL,
    report     jsonb NOT NULL
);
CREATE INDEX scan_runs_root ON scan_runs (root, started_at DESC);
