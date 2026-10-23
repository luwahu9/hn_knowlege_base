import {el, api, post, empty, docUrl, pages, table} from './dom.js';
import {reviewControls} from './review.js';
import {evidence} from './evidence.js';
import {can} from './auth_state.js';

export async function referenceTitles(data, known = []) {
  const titles = new Map(known.map(row => [row.version_id, row.title]));
  for (const row of data.links || []) {
    titles.set(row.source, row.source_title); titles.set(row.target, row.target_title);
  }
  const needed = new Set([...(data.pending || []), ...(data.unresolved || [])].map(row => row.version_id));
  // 复用已取得的标题；缺失时用可点击的版本号，避免为标题下载整份分块。
  for (const version of needed) if (!titles.has(version)) titles.set(version, version);
  return titles;
}

export const relationKinds = {cites: '引用', supersedes: '替代', conflicts: '冲突'};
const verdicts = {conflict: '冲突', review: '待判断'};

function highlightedQuote(text, detail) {
  const quote = el('blockquote');
  if (!detail) { quote.textContent = text; return quote; }
  // 逐字符 NFKC、去空白，并把矛盾片段的位置映射回原文。
  function normalized(value) {
    let key = '', index = 0;
    const offsets = [];
    for (const char of value) {
      for (const part of char.normalize('NFKC')) {
        if (/[\s\u0085\u001c-\u001f]/u.test(part)) continue;
        key += part;
        for (let unit = 0; unit < part.length; unit++) offsets.push([index, index + char.length]);
      }
      index += char.length;
    }
    return {key, offsets};
  }
  const source = normalized(text), target = normalized(detail).key;
  const index = target ? source.key.indexOf(target) : -1;
  if (index < 0) { quote.textContent = text; return quote; }
  const start = source.offsets[index][0], end = source.offsets[index + target.length - 1][1];
  quote.append(document.createTextNode(text.slice(0, start)), el('mark', {}, text.slice(start, end)),
    document.createTextNode(text.slice(end)));
  return quote;
}

function referenceEvidence(row, titles) {
  return evidence({...row, title: titles.get(row.version_id) || row.version_id}, {status: row.decided ? '人工确认' : '引用已定位'});
}

function conflictEvidence(row, actionable = false) {
  const article = el('article', {className: 'panel conflict-evidence'},
    el('span', {className: 'badge link-verdict-' + row.verdict}, verdicts[row.verdict]),
    row.decided ? el('span', {className: 'badge kg-pass'}, '人工确认') : null,
    el('p', {className: 'conflict-reason'}, row.reason || '未提供理由'));
  for (const [label, face, detail] of [['甲', row.a, row.detail_a], ['乙', row.b, row.detail_b]]) {
    article.append(el('section', {className: 'conflict-face'}, el('h3', {},
      label, ' · ', el('a', {href: docUrl(face.version_id)}, face.title)),
      evidence({...face, verdict: row.verdict === 'review' ? 'review' : 'reject'},
        {quote: highlightedQuote(face.quote, detail), status: row.decided ? '人工确认' : verdicts[row.verdict]})));
  }
  if (actionable) article.append(reviewControls([
    {text: '确认冲突', kind: 'conflict_confirm', target: row.id},
    {text: '不冲突', kind: 'conflict_dismiss', target: row.id},
  ]));
  return article;
}

export function relationDetail(edge, data) {
  const source = edge.source.replace(/^doc:/, ''), target = edge.target.replace(/^doc:/, '');
  const article = el('article', {className: 'knowledge-detail relation-detail'}, el('h2', {}, relationKinds[edge.kind]));
  if (edge.kind === 'conflicts') {
    const rows = data.conflicts.filter(row => [row.version_a, row.version_b].sort().join('|') === [source, target].sort().join('|'));
    article.append(...rows.map(row => conflictEvidence(row)));
    if (!rows.length) article.append(empty('暂无关系证据'));
  } else {
    const rows = data.links.filter(row => row.source === source && row.target === target && row.kind === edge.kind);
    if (!rows.length) article.append(empty('暂无关系证据'));
    else {
      article.append(el('div', {className: 'kg-link-list'}, el('a', {href: docUrl(source)}, rows[0].source_title),
        el('span', {className: 'muted'}, '↓ ' + relationKinds[edge.kind]),
        el('a', {href: docUrl(target)}, rows[0].target_title)));
      const titles = new Map(rows.flatMap(row => [[row.source, row.source_title], [row.target, row.target_title]]));
      article.append(...rows.map(row => referenceEvidence(row, titles)));
    }
  }
  return article;
}

export function linkReview(rows, titles) {
  return rows.length ? el('div', {className: 'review-list'}, rows.map(row =>
    el('article', {className: 'panel review-reference'},
      el('div', {className: 'review-card-top'}, el('div', {}, el('span', {className: 'badge kg-review'}, '待确认引用'),
        el('h3', {}, row.title, ' · ', row.reverse ? '被替代' : relationKinds[row.kind])),
      reviewControls([{text: '库中没有', kind: 'reference_set', target: row.id, value: ''}])),
      referenceEvidence(row, titles), el('h3', {}, '候选文档'),
      el('div', {className: 'kg-link-list'}, row.candidates.map(candidate => el('div', {className: 'review-candidate'},
        el('a', {href: docUrl(candidate.version_id)}, candidate.title),
        reviewControls([{text: '就是这份', kind: 'reference_set', target: row.id, value: candidate.version_id}])))))))
    : empty('暂无待确认引用', '同名或只有包含关系的书名会列在这里，由人工核对。');
}

export function conflictReview(rows) {
  return rows.length ? el('div', {className: 'conflict-list'}, rows.map(row => conflictEvidence(row, true)))
    : empty('暂无文档冲突', '发现冲突或需要进一步判断的文档片段会显示在这里。');
}

export function linkCard(data) {
  const status = data.status;
  return el('a', {href: '#/documents', className: 'panel knowledge-summary relation-summary'},
    el('div', {}, el('h2', {}, '文档关系'), el('p', {className: 'muted'}, '已分析 ' + status.extracted + ' / ' + status.published + ' 份已发布文档')),
    el('div', {className: 'knowledge-counts'}, [['引用', status.cites], ['替代', status.supersedes],
      ['冲突', status.conflicts], ['待确认引用', status.pending]].map(([label, count]) =>
      el('span', {}, el('strong', {}, count), ' ' + label))), el('span', {}, '到文档库查看关系 →'));
}

export async function documentRelations(version) {
  const data = await api('/api/documents/' + encodeURIComponent(version) + '/links');
  const panel = el('section', {className: 'panel document-relations compact'}, el('h2', {}, '文档关系'));
  const groups = [
    ['引用', data.links.filter(row => row.kind === 'cites' && row.source === version)],
    ['被引用', data.links.filter(row => row.kind === 'cites' && row.target === version)],
    ['替代', data.links.filter(row => row.kind === 'supersedes' && [row.source, row.target].includes(version))],
  ];
  for (const [label, rows] of groups) {
    panel.append(el('h3', {}, label, `（${rows.length}）`));
    for (const row of rows) {
      const outgoing = row.source === version;
      panel.append(el('article', {className: 'document-relation'}, el('p', {},
        label === '替代' ? (outgoing ? '本文档替代 ' : '本文档被替代于 ') : (outgoing ? '引用 ' : '来自 '),
        el('a', {href: docUrl(outgoing ? row.target : row.source)}, outgoing ? row.target_title : row.source_title)),
        referenceEvidence(row, new Map([[row.source, row.source_title], [row.target, row.target_title]]))));
    }
    if (!rows.length) panel.append(el('p', {className: 'muted'}, '暂无此类关系'));
  }
  const conflicts = data.conflicts.filter(row => [row.version_a, row.version_b].includes(version));
  panel.append(el('h3', {}, `冲突（${conflicts.length}）`), ...conflicts.map(row => conflictEvidence(row)));
  if (!conflicts.length) panel.append(el('p', {className: 'muted'}, '暂无冲突记录'));
  return panel;
}

export async function linksPanel(onUpdated, known = []) {
  let data = await api('/api/links?limit=1');
  const titles = await referenceTitles(data, known);
  let disposed = false, timer = null, wasActive = !data.status.jobs.idle;
  let offset = 0, loading = false, finished = false;
  const controller = new AbortController();
  const panel = el('div', {className: 'panel relations-status'}), summary = el('strong');
  const progress = el('p', {className: 'muted', 'aria-live': 'polite'}), errorBox = el('div', {role: 'alert'});
  const extract = el('button', {className: 'button', type: 'button', hidden: !can('review')}, '分析文档关系');
  const judge = el('button', {className: 'button', type: 'button', hidden: !can('review')}, '检查文档冲突');
  const candidates = el('span', {className: 'muted'}), failures = el('details', {className: 'kg-failures'});
  const unresolved = el('details', {className: 'unresolved-references'});
  const unresolvedTitle = el('summary'), unresolvedRows = el('div', {className: 'unresolved-list'});
  const loaded = el('p', {className: 'muted', 'aria-live': 'polite'});
  const pageError = el('p', {className: 'error-text', role: 'alert'});
  const more = el('button', {className: 'button', type: 'button'}, '加载更多');
  unresolved.append(unresolvedTitle, unresolvedRows, loaded, pageError, more);
  panel.append(summary, el('div', {className: 'relations-actions'}, extract, judge, candidates), progress, failures, unresolved, errorBox);
  function paintPage() {
    const total = data.status.unresolved;
    unresolvedTitle.textContent = '未收录引用 ' + total + ' 条';
    loaded.textContent = '已显示 ' + offset + ' / ' + total + ' 条';
    more.hidden = finished || offset >= total;
    more.disabled = loading;
    more.textContent = loading ? '正在加载…' : '加载更多';
  }
  async function loadMore() {
    if (disposed || loading || finished) return;
    loading = true; pageError.textContent = ''; paintPage();
    try {
      const next = await api('/api/links?category=unresolved&offset=' + offset + '&limit=100', {signal: controller.signal});
      const nextTitles = await referenceTitles(next, known);
      if (disposed) return;
      for (const [version, title] of nextTitles) titles.set(version, title);
      data = next;
      unresolvedRows.append(...next.unresolved.map(row => referenceEvidence(row, titles)));
      offset += next.unresolved.length;
      finished = !next.unresolved.length || offset >= next.status.unresolved;
      paint();
    } catch (error) { if (!disposed) pageError.textContent = error.message; }
    finally { loading = false; if (!disposed) paintPage(); }
  }
  more.addEventListener('click', loadMore);
  function paint() {
    const s = data.status, jobs = s.jobs;
    summary.textContent = '文档关系：已分析 ' + s.extracted + ' / ' + s.published + ' 份 · 引用 ' + s.cites + ' · 替代 ' + s.supersedes +
      ' · 冲突 ' + s.conflicts + '（待判断 ' + s.review + '）';
    candidates.textContent = '待判断 ' + (s.candidates === null ? '—' : s.candidates === 10000 ? '10000+' : s.candidates) + ' 对';
    extract.disabled = !jobs.idle;
    judge.disabled = jobs.queued.includes('conflicts');
    progress.textContent = jobs.idle ? '分析队列空闲' : '排队 ' + jobs.queued.length + ' · 当前：' +
      (jobs.running?.startsWith('refs:') ? (titles.get(jobs.running.slice(5)) || jobs.running) : jobs.running === 'conflicts' ? '检查文档冲突' : '准备中');
    failures.hidden = !jobs.failed.length;
    failures.replaceChildren(el('summary', {}, jobs.failed.length + ' 个条目失败（展开查看）'),
      ...jobs.failed.map(row => el('p', {}, row.key, '：', row.error)));
    // 状态轮询不替换已加载的分页内容，也不收起展开的列表。
    paintPage();
  }
  async function poll() {
    clearTimeout(timer);
    try {
      const next = await api('/api/links?limit=1', {signal: controller.signal});
      if (disposed) return;
      data = next; paint();
      if (wasActive && data.status.jobs.idle) await onUpdated();
      wasActive = !data.status.jobs.idle;
    } catch (error) { if (!disposed) errorBox.replaceChildren(el('div', {className: 'error-banner'}, error.message)); }
    if (!disposed && (!data.status.jobs.idle || wasActive)) timer = setTimeout(poll, 2000);
  }
  async function trigger(path) {
    errorBox.replaceChildren();
    extract.disabled = judge.disabled = true;
    progress.textContent = '正在提交文档关系作业…';
    try { await post(path, {}); wasActive = true; await poll(); }
    catch (error) { if (!disposed) { paint(); errorBox.replaceChildren(el('div', {className: 'error-banner'}, error.message)); } }
  }
  extract.addEventListener('click', () => trigger('/api/links/extract'));
  judge.addEventListener('click', () => trigger('/api/links/conflicts'));
  paint();
  await loadMore();
  return {element: panel, mount: () => { if (!data.status.jobs.idle) timer = setTimeout(poll, 2000); },
    dispose: () => { disposed = true; clearTimeout(timer); controller.abort(); }};
}
