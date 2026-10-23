-- 批次 19：小安对话记录（docs/spec-35-conversation.md）。永久保留，本人可删。
CREATE TABLE conversations (
    conversation_id  text COLLATE "C" PRIMARY KEY,
    owner            text NOT NULL,
    title            text NOT NULL,
    created_at       text NOT NULL,
    updated_at       text NOT NULL,
    turns            integer NOT NULL
);
CREATE INDEX conversations_owner ON conversations (owner, updated_at DESC, conversation_id DESC);

CREATE TABLE conversation_turns (
    conversation_id  text COLLATE "C" NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
    ordinal          integer NOT NULL,
    data             jsonb NOT NULL,
    PRIMARY KEY (conversation_id, ordinal)
);
