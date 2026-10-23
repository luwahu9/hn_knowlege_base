-- 批次 23b：实体之间的关系（docs/spec-41-entity-relations.md）。随抽取结果整体替换，随 knowledge_docs 级联删除。
CREATE TABLE entity_relations (
    relation_id    text COLLATE "C" PRIMARY KEY,
    version_id     text COLLATE "C" NOT NULL REFERENCES knowledge_docs(version_id) ON DELETE CASCADE,
    ordinal        integer NOT NULL,
    kind           text NOT NULL,
    source_entity  text COLLATE "C" NOT NULL,
    target_entity  text COLLATE "C" NOT NULL,
    data           jsonb NOT NULL
);
CREATE INDEX entity_relations_version ON entity_relations (version_id);
CREATE INDEX entity_relations_source ON entity_relations (source_entity);
CREATE INDEX entity_relations_target ON entity_relations (target_entity);
