import {el, badge, pages, docUrl} from './dom.js';
import {graphUrl} from './knowledge.js';

const kinds = [['document', '文档'], ['mail', '邮件'], ['event', '知识事件']];
const keywordLabels = {dictionary: '词典', model: '模型', none: '未识别'};

function sourceUrl(source) {
  if (source.kind === 'mail') return '#/mail/' + encodeURIComponent(source.mail_id);
  const url = docUrl(source.version_id, source.chunk_id);
  return source.page_start == null ? url : url + (url.includes('?') ? '&' : '?') + 'page=' + encodeURIComponent(source.page_start);
}

function entityLinks(entities) {
  return entities.map(entity => el('a', {href: graphUrl('ent:' + entity.entity_id), className: 'cross-entity'}, entity.text));
}

function sourceCard(source, quote = null) {
  const label = kinds.find(([kind]) => kind === source.kind)?.[1] || '资料';
  return el('div', {className: quote === null ? 'source-row cross-source' : 'citation-card cross-source'},
    el('span', {className: 'ref-number'}, source.ref),
    el('div', {className: 'cross-source-content'},
      el('div', {className: 'cross-source-title'}, el('span', {className: 'cross-kind'}, label),
        el('a', {href: sourceUrl(source)}, source.title || '未标注标题')),
      el('small', {className: 'cross-source-time'}, '时间：', source.when || '未知'),
      source.kind === 'mail' ? el('small', {}, source.sender || '发件人未标注', ' · ', source.folder || '文件夹未标注')
        : el('small', {}, pages(source.page_start, source.page_end), ' · ', source.section || '未标注章节'),
      source.score == null ? null : el('small', {}, '重排分数 ', Number(source.score).toFixed(3)),
      quote === null ? null : el('blockquote', {className: 'cross-quote'}, quote)));
}

function sourceGroups(rows, entities, citations = false) {
  return kinds.map(([kind, label]) => {
    const selected = rows.filter(row => (citations ? row.source : row).kind === kind);
    if (!selected.length) return null;
    return el('section', {className: 'cross-source-group cross-' + kind},
      el('h3', {}, `${label}（${selected.length}）`),
      kind === 'event' && entities.length ? el('div', {className: 'cross-event-entities'},
        el('span', {className: 'muted'}, '本次识别实体：'), entityLinks(entities)) : null,
      selected.map(row => sourceCard(citations ? row.source : row, citations ? row.quote : null)));
  });
}

export function crossAnswerView(result) {
  if (result.hidden) return el('p', {className: 'warning-banner'}, result.answer);
  const answer = el('div', {className: 'answer-card cross-answer'}, el('div', {className: 'answer-heading'},
    el('span', {className: 'an-avatar small'}, '安'), el('strong', {}, '小安'), badge(result.status)));
  if (result.status === 'ungrounded') answer.append(el('div', {className: 'warning-banner'}, '以下回答未能在资料中找到出处，请谨慎参考'));
  answer.append(el('div', {className: 'answer-text'}, result.answer));
  if (!result.reranked) answer.append(el('p', {className: 'muted cross-rerank'}, '未重排'));
  answer.append(
    el('div', {className: 'cross-entities'}, el('span', {}, '实体来源：', keywordLabels[result.keywords_from] || '未识别'),
      result.entities.length ? entityLinks(result.entities) : el('span', {className: 'muted'}, '未识别到实体')),
    el('div', {className: 'cross-candidates'}, el('strong', {}, '候选'),
      el('span', {}, `文档 ${result.candidates.documents || 0}`),
      el('span', {}, `邮件 ${result.candidates.mails || 0}`),
      el('span', {}, `知识事件 ${result.candidates.events || 0}`)));
  if (result.citations.length) answer.append(el('div', {className: 'citations'},
    el('div', {className: 'section-label'}, '参考出处'), sourceGroups(result.citations, result.entities, true)));
  const sources = el('details', {className: 'sources'}, el('summary', {}, `交给模型的资料（${result.sources.length}）`),
    sourceGroups(result.sources, result.entities));
  answer.append(sources, el('div', {className: 'answer-footer'},
    `${result.model || '未调用模型'}${result.seconds == null ? '' : ' · ' + Number(result.seconds).toFixed(1) + ' 秒'}`,
    result.dropped_citations ? ` · 已过滤 ${result.dropped_citations} 条无效引用` : ''));
  return answer;
}
