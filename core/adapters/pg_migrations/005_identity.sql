-- 批次 16a：账号、用户组、会话、文件夹授权。规格见 docs/spec-28-auth.md。

CREATE TABLE users (
    user_id       text COLLATE "C" PRIMARY KEY,
    username      text COLLATE "C" NOT NULL UNIQUE,
    display_name  text NOT NULL,
    role          text NOT NULL CHECK (role IN ('admin', 'reviewer', 'member')),
    active        boolean NOT NULL,
    source        text NOT NULL,
    password_hash text,
    failures      integer NOT NULL DEFAULT 0,
    last_failure  timestamptz
);

CREATE TABLE user_groups (
    group_id text COLLATE "C" PRIMARY KEY,
    name     text COLLATE "C" NOT NULL UNIQUE
);

CREATE TABLE group_members (
    group_id text COLLATE "C" NOT NULL REFERENCES user_groups(group_id) ON DELETE CASCADE,
    user_id  text COLLATE "C" NOT NULL REFERENCES users(user_id),
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX group_members_user ON group_members (user_id);

CREATE TABLE sessions (
    token_hash text COLLATE "C" PRIMARY KEY,
    user_id    text COLLATE "C" NOT NULL REFERENCES users(user_id),
    created_at timestamptz NOT NULL,
    last_seen  timestamptz NOT NULL
);
CREATE INDEX sessions_user ON sessions (user_id);

CREATE TABLE auth_audit (
    seq    bigserial PRIMARY KEY,
    at     timestamptz NOT NULL,
    actor  text COLLATE "C",
    action text NOT NULL,
    target text COLLATE "C",
    detail jsonb NOT NULL
);
CREATE INDEX auth_audit_target ON auth_audit (target, seq);

-- 授权：文件夹 ID 即规范路径，文档 ID 即 version_id；用户组可以是内置的 'all'，因此不加外键。
CREATE TABLE folder_acl (
    path   text COLLATE "C" PRIMARY KEY,
    groups text[] COLLATE "C" NOT NULL
);
CREATE TABLE document_acl (
    version_id text COLLATE "C" PRIMARY KEY,
    groups     text[] COLLATE "C" NOT NULL
);
CREATE TABLE acl_audit (
    seq    bigserial PRIMARY KEY,
    at     timestamptz NOT NULL,
    actor  text COLLATE "C" NOT NULL,
    kind   text NOT NULL CHECK (kind IN ('folder', 'document')),
    target text COLLATE "C" NOT NULL,
    groups text[] COLLATE "C",
    reason text
);
CREATE INDEX acl_audit_target ON acl_audit (target, seq);
-- 授权修订号：每次授权变更在同一事务里加一（UPDATE … RETURNING，行锁使并发变更串行）。
CREATE TABLE acl_revision (
    id       integer PRIMARY KEY CHECK (id = 1),
    revision bigint NOT NULL
);
INSERT INTO acl_revision VALUES (1, 0);

CREATE TABLE index_tags (
    version_id text COLLATE "C" PRIMARY KEY,
    tags       text[] COLLATE "C" NOT NULL
);
