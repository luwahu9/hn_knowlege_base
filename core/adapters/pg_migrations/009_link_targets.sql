-- 批次 18b：引用目标的物化（docs/spec-33-link-targets.md）。
-- link_target_docs：物化时的已发布文档与规范化标题；link_targets：每条引用的匹配（exact、fuzzy 为版本号，升序）。
-- 不设外键：引用重提取与同步可能并发，同步时以引用表为准增删，过期的行在下一次同步中删除。
CREATE TABLE link_target_docs (
    version_id  text COLLATE "C" PRIMARY KEY,
    title_key   text NOT NULL
);

CREATE TABLE link_targets (
    ref_id      text COLLATE "C" PRIMARY KEY,
    version_id  text COLLATE "C" NOT NULL,
    title_key   text NOT NULL,
    exact       text[] NOT NULL,
    fuzzy       text[] NOT NULL
);

CREATE TABLE link_target_meta (
    id       integer PRIMARY KEY CHECK (id = 1),
    version  bigint NOT NULL
);
INSERT INTO link_target_meta (id, version) VALUES (1, 0);
