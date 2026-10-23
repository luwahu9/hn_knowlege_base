import {el} from './dom.js';
import {evidence} from './evidence.js';
import {reviewControls} from './review.js';

// 箭头遵循 source → target；入边的名词说明对方与当前实体的关系。
export const relationTypes = [
  {key: 'superior', label: '上下级', out: '下级 → ', in: '← 上级：', color: '--c-person', dash: []},
  {key: 'member', label: '分属', out: '分属 → ', in: '← 成员：', color: '--c-org', dash: [9, 3]},
  {key: 'located', label: '位于', out: '位于 → ', in: '← 位于此地：', color: '--c-location', dash: [2, 4]},
  {key: 'participant', label: '参与', out: '参与 → ', in: '← 参与者：', color: '--c-named-event', dash: [10, 3, 2, 3]},
  {key: 'award', label: '获得', out: '获得 → ', in: '← 获得者：', color: '--c-honor', dash: [4, 3]},
];
export const relationType = key => relationTypes.find(type => type.key === key);
export const relationSummary = edge => `${edge.label} · ${(edge.roles || []).join('、') || '未注明职务或角色'} · ${edge.count} 条`;

export function relationList(row, graphUrl = id => '#/graph?' + new URLSearchParams({focus: id, depth: 1})) {
  const root = el('section', {className: 'entity-relations'}, el('h3', {}, '实体关系'));
  const rows = row.relations || [];
  if (!rows.length) root.append(el('p', {className: 'muted'}, '暂无关系'));
  for (const type of relationTypes) {
    const group = rows.filter(item => item.kind === type.key);
    if (!group.length) continue;
    root.append(el('h4', {}, type.label), ...group.map(item => el('article', {className: 'entity-relation', 'data-relation-id': item.relation_id},
      el('div', {className: 'relation-direction'}, type[item.direction],
        el('a', {href: graphUrl('ent:' + item.other.id)}, item.other.canonical),
        item.role ? `（${item.role}）` : null),
      item.time_expr ? el('p', {className: 'muted'}, '时间：', item.time_expr) : null,
      evidence(item), reviewControls([{text: '否决', kind: 'relation_reject', target: item.relation_id}]))));
  }
  if (row.relations_truncated) root.append(el('p', {className: 'truncation-note'}, `只显示前 ${rows.length} 条`));
  return root;
}
