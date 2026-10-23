-- 可见实体的引用判定包含重复和拒绝事件，不能使用排除重复事件的既有索引。
-- 只读取参与者和版本即可判定可见性，避免扫描带有宽 JSON 的事件行。
CREATE INDEX events_scope_subject ON events (eff_subject, version_id)
    WHERE eff_subject IS NOT NULL;
CREATE INDEX events_scope_object ON events (eff_object, version_id)
    WHERE eff_object IS NOT NULL;
