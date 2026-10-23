import {el, pages, docUrl} from './dom.js';

export function evidence(row, options = {}) {
  const status = options.status || (row.decided ? '人工确认' : row.verdict === 'review' ? '待审' : row.verdict === 'reject' ? '已驳回' : '核验通过');
  return el('div', {className: 'evidence'},
    options.quote || el('blockquote', {}, row.quote || '未提供引文'),
    el('div', {className: 'evidence-source'}, el('span', {}, row.title || row.source || row.version_id),
      el('span', {}, pages(row.page_start, row.page_end), ' · ', row.section_path || row.section || '未标注章节'),
      el('span', {className: 'evidence-version'}, row.version_id),
      el('span', {className: `badge kg-${row.verdict || 'pass'}`}, status),
      el('a', {href: docUrl(row.version_id, row.chunk_id)}, '查看原文')));
}
