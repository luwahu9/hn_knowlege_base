-- 批次 17a：邮件扫描目录。规格见 docs/spec-30-mail.md 第三节；邮件内容在 Elasticsearch，不在此处。

-- 每个邮件根下每个已扫描目录的最近状态。dir 为相对根的规范路径，根为 ''。
CREATE TABLE mail_dirs (
    root      text COLLATE "C" NOT NULL,
    dir       text COLLATE "C" NOT NULL,
    mtime_ns  bigint NOT NULL,
    children  text[] COLLATE "C" NOT NULL,
    mails     integer NOT NULL CHECK (mails >= 0),
    PRIMARY KEY (root, dir)
);

-- 每个已处理文件的最近状态（可达数亿行，只保留判定增量所需的列）。mail_id 为空表示内容不是邮件。
CREATE TABLE mail_files (
    root      text COLLATE "C" NOT NULL,
    dir       text COLLATE "C" NOT NULL,
    name      text COLLATE "C" NOT NULL,
    size      bigint NOT NULL CHECK (size >= 0),
    mtime_ns  bigint NOT NULL,
    mail_id   text COLLATE "C",
    PRIMARY KEY (root, dir, name)
);

-- 扫描报告。
CREATE TABLE mail_runs (
    scan_id    text COLLATE "C" PRIMARY KEY,
    root       text COLLATE "C" NOT NULL,
    started_at text NOT NULL,
    report     jsonb NOT NULL
);
CREATE INDEX mail_runs_root ON mail_runs (root, started_at DESC, scan_id DESC);
