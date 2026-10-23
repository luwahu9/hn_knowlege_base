-- 批次 23a：实体分类（docs/spec-40-entity-taxonomy.md）。原来的“媒体”并入组织，子类为“媒体”；
-- 事件参与者类型中的 media 同样改为 organization。其他记录补上 subtype（null）。
UPDATE entities SET type = 'organization',
       data = jsonb_set(jsonb_set(data, '{type}', '"organization"'), '{subtype}', '"媒体"')
 WHERE type = 'media';
UPDATE entities SET data = data || '{"subtype": null}'::jsonb WHERE NOT data ? 'subtype';
UPDATE events SET data = jsonb_set(data, '{subject_type}', '"organization"') WHERE data->>'subject_type' = 'media';
UPDATE events SET data = jsonb_set(data, '{object_type}', '"organization"') WHERE data->>'object_type' = 'media';
