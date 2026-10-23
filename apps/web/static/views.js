import {libraryTabs, seriesSelection, documentSeries} from './series.js';
import {conversationView} from './conversations.js';
import {documentRelations, linksPanel, linkCard, linkReview, conflictReview, referenceTitles} from './links.js';
import {reviewControls, undoControl, refreshReview, documentReviewControls, documentReviewStatus} from './review.js';
import {el, api, post, labels, badge, timeText, pages, docUrl, sourceLink, empty, heading, table, docTable} from './dom.js';
import {knowledgeCard, entityReview, eventReview} from './knowledge.js';
import {scanPanel} from './scan.js';
import {mailScanPanel} from './mail.js';
import {can, identityEpoch, getIdentity} from './auth_state.js';
import {documentPermissions} from './admin.js';
import {lifecycleControls, versionHistory, versionReview} from './lifecycle.js';
import {purgeControls, purgeReport, tombstoneView} from './purge.js';

let reviewTab = 'documents';
const documentFilter = {q: '', status: '', offset: 0};
let documentRequest = 0;
export function clearPrivateState() {
  reviewTab = 'documents';
  documentFilter.q = documentFilter.status = '';
  documentFilter.offset = 0;
  documentRequest++;
}

export function askView(query) {
  return conversationView(query);
}

export async function overviewView() {
  const [data, knowledge, links, tasks] = await Promise.all([api('/api/overview'),
    can('knowledge') ? api('/api/knowledge') : null, can('knowledge') ? api('/api/links?limit=1') : null,
    can('review') ? api('/api/tasks') : null]);
  const page = el('section', {}, heading('知识总览', '了解知识库的资料规模与最近导入情况。'));
  const cards = [ ['文档总数', data.documents, '已登记的全部资料'], ['已发布分块', data.chunks, '可用于检索的内容单元'],
    ['已发布文档', data.by_status.published || 0, '已进入检索引擎'], ...(can('review') ? [['未收录问题', data.unanswered, '需要补充资料的问题']] : []) ];
  page.append(el('div', {className: 'stats'}, cards.map(([title, value, note], i) => el('div', {className: 'stat-card'},
    el('div', {className: 'stat-title'}, title, el('span', {className: 'stat-index'}, `0${i + 1}`)),
    el('strong', {}, value.toLocaleString('zh-CN')), el('small', {}, note)))));
  page.append(el('div', {className: 'status-strip'}, el('strong', {}, '文档状态'),
    ['published', 'withdrawn', 'superseded', 'qc_blocked', 'unsupported', 'no_chunks', 'error'].map(status => el('span', {}, badge(status), ' ', data.by_status[status] || 0))));
  if (tasks) {
    const taskPanel = el('div', {className: 'panel task-summary'}, el('h2', {}, '任务概况'));
    const taskRows = el('div', {className: 'task-summary-rows'});
    const taskError = el('p', {className: 'error-text', role: 'alert'});
    let active = false, disposed = false, timer = null, pendingTasks = false;
    const controller = new AbortController();
    function paintTasks(values) {
      pendingTasks = Object.values(values).some(row => row.queued > 0 || row.running > 0);
      taskRows.replaceChildren(...Object.entries({ingest: '文档导入', ...(can('knowledge') ? {extract: '实体与事件抽取', links: '文档连线'} : {}),
        ...(can('scan') ? {scan: '目录扫描'} : {}), ...(getIdentity()?.features?.mail ? {mail_scan: '邮件扫描'} : {}),
        ...(can('manage_acl') ? {acl_sync: '检索标签同步'} : {})}).map(([kind, title]) => {
        const row = values[kind];
        if (!row) return null;
        return el('div', {className: 'task-summary-row'}, el('strong', {}, title),
          el('span', {}, '排队 ', row.queued), el('span', {}, '执行中 ', row.running),
          el('span', {className: row.failed > 0 ? 'task-failed' : 'muted'}, '失败 ', row.failed));
      }).filter(Boolean));
    }
    function schedule() { if (active && pendingTasks) timer = setTimeout(refreshTasks, 3000); }
    async function refreshTasks() {
      try {
        const values = await api('/api/tasks', {signal: controller.signal});
        if (!active) return;
        taskError.textContent = '';
        paintTasks(values);
      } catch (error) { if (active) taskError.textContent = error.message; }
      finally { schedule(); }
    }
    paintTasks(tasks);
    taskPanel.append(taskRows, taskError);
    page.mount = () => { if (!disposed) { active = true; schedule(); } };
    page.dispose = () => { disposed = true; active = false; clearTimeout(timer); controller.abort(); };
    page.append(taskPanel);
  }
  if (knowledge) page.append(knowledgeCard(knowledge));
  if (links) page.append(linkCard(links));
  page.append(el('div', {className: 'panel'}, el('div', {className: 'panel-heading'}, el('h2', {}, '最近导入'),
    el('a', {href: '#/documents', className: 'text-link'}, '查看全部文档 →')), docTable(data.recent)));
  return page;
}

export async function documentsView() {
  const request = ++documentRequest, epoch = identityEpoch(), filter = {...documentFilter};
  const current = () => request === documentRequest && epoch === identityEpoch() &&
    Object.keys(filter).every(key => filter[key] === documentFilter[key]);
  const query = new URLSearchParams({...filter, limit: 50});
  let data = await api(`/api/documents?${query}`);
  if (!current()) return el('section');
  if (data.offset > 0 && data.offset >= data.total) {
    filter.offset = documentFilter.offset = data.total ? Math.floor((data.total - 1) / 50) * 50 : 0;
    query.set('offset', filter.offset);
    data = await api(`/api/documents?${query}`);
    if (!current()) return el('section');
  }
  const {rows, total, offset, limit} = data;
  const page = el('section', {}, heading('文档库', '查看文档状态、转换质检结果和可追溯的原文分块。',
    can('upload') ? el('button', {className: 'button', onclick: () => document.querySelector('#open-folder-upload').click()}, '导入文件夹') : null));
  page.append(libraryTabs());
  const selection = await seriesSelection();
  const hiddenPanel = () => ({element: el('span', {hidden: true}), mount() {}, dispose() {}});
  const relations = can('knowledge') ? await linksPanel(refreshReview, rows) : hiddenPanel();
  const scans = can('scan') ? await scanPanel() : hiddenPanel();
  const mailScans = can('scan') && getIdentity()?.features?.mail ? await mailScanPanel() : hiddenPanel();
  page.append(relations.element);
  page.append(el('div', {className: 'server-scan-panels'}, scans.element, mailScans.element));
  page.mount = () => { relations.mount?.(); scans.mount(); mailScans.mount(); };
  page.dispose = () => { relations.dispose?.(); scans.dispose(); mailScans.dispose(); selection.dispose(); };
  const select = el('select', {name: 'status', 'aria-label': '按文档状态筛选'}, el('option', {value: ''}, '全部状态'),
    ['published', 'withdrawn', 'superseded', 'purged', 'qc_blocked', 'unsupported', 'no_chunks', 'error'].map(status => el('option', {value: status}, labels[status])));
  select.value = documentFilter.status;
  const input = el('input', {name: 'q', type: 'search', placeholder: '按标题或文件名查找', value: documentFilter.q, 'aria-label': '搜索文档标题'});
  const refresh = () => { documentFilter.q = input.value; documentFilter.status = select.value; documentFilter.offset = 0; window.dispatchEvent(new Event('kb:updated')); };
  select.addEventListener('change', refresh);
  page.append(el('form', {className: 'filter-bar', onsubmit: event => { event.preventDefault(); refresh(); }},
    el('div', {className: 'filters'}, select, input, el('button', {className: 'button', type: 'submit'}, '查找')),
    el('span', {className: 'muted'}, `共 ${total} 份文档`)), selection.element, el('div', {className: 'panel'}, docTable(rows, {selection: can('review') ? selection.checkbox : null})));
  const turn = next => { documentFilter.offset = next; window.dispatchEvent(new Event('kb:updated')); };
  page.append(el('div', {className: 'document-pagination', 'aria-label': '文档库翻页'},
    el('span', {className: 'muted'}, `共 ${total} 份 · 第 ${total ? Math.floor(offset / limit) + 1 : 0} / ${Math.ceil(total / limit)} 页`),
    el('button', {className: 'button', type: 'button', disabled: offset === 0, onclick: () => turn(Math.max(0, offset - limit))}, '上一页'),
    el('button', {className: 'button', type: 'button', disabled: offset + limit >= total, onclick: () => turn(offset + limit)}, '下一页')));
  return page;
}

function qcList(record) {
  if (!record.qc?.length) return el('p', {className: 'muted'}, '无质检项记录');
  return el('ul', {className: 'qc-list'}, record.qc.map(item => el('li', {},
    el('span', {className: `badge ${item.severity === 'blocking' ? 'status-qc_blocked' : 'status-insufficient'}`},
      item.severity === 'blocking' ? '阻断' : '警告'), el('strong', {}, item.code),
    el('p', {}, item.detail || item.message || ''))));
}

export async function detailView(version, query) {
  const data = await api(`/api/documents/${encodeURIComponent(version)}`);
  const record = data.document;
  if (record.status === 'purged') return tombstoneView(record);
  const page = el('section', {}, el('a', {href: '#/documents', className: 'back-link'}, '← 返回文档库'),
    heading(record.title, record.filename, badge(record.status)));
  const metadata = [['页数', record.page_count ?? '—'], ['分块数', record.chunk_count], ['OCR', record.ocr ? '已开启' : '未开启'],
    ['来源', record.origin === 'seed' ? '种子资料' : record.origin === 'scan' ? '目录扫描' : '上传'], ['文件夹', record.folder || '根目录'],
    ['导入时间', timeText(record.imported_at)], ['版本号', record.version_id]];
  page.append(el('dl', {className: 'metadata'}, metadata.map(([label, value]) => el('div', {}, el('dt', {}, label), el('dd', {}, value)))));
  page.append(documentSeries(data.series) || el('span', {hidden: true}));
  const lifecycle = lifecycleControls(record);
  const purge = purgeControls(record, result => {
    lifecycle.dispose(); purge.dispose();
    page.replaceChildren(tombstoneView(result.document), purgeReport(result.report));
  });
  page.append(lifecycle, purge, await versionHistory(version));
  page.dispose = () => { lifecycle.dispose(); purge.dispose(); };
  if (can('review') && (record.review || record.retry_of)) page.append(documentReviewStatus(record));
  if (record.detail || record.error) page.append(el('div', {className: 'warning-banner'}, record.error || record.detail));
  const verify = el('div', {className: 'panel compact'}, el('h2', {}, '索引校验'));
  if (['withdrawn', 'superseded'].includes(record.status)) verify.append(el('p', {className: 'muted'}, '此版本当前不参与检索；恢复后重新写入并核对索引。'));
  else if (record.verify) verify.append(el('div', {className: 'verify-values'},
    el('span', {}, record.verify.indexed ? '✓ 已索引' : '未索引'), el('span', {}, `${record.verify.chunk_count} 个分块`),
    el('span', {}, record.verify.sample_hit ? '✓ 抽样检索命中' : '抽样检索未命中')));
  else verify.append(el('p', {className: record.verify_error ? 'error-text' : 'muted'}, record.verify_error || '尚无索引校验结果'));
  page.append(el('div', {className: 'detail-checks'}, el('div', {className: 'panel compact'}, el('h2', {}, '转换质检'), qcList(record)), verify));
  if (can('knowledge')) page.append(await documentRelations(version));
  if (can('manage_acl') && getIdentity()?.auth_enabled) page.append(await documentPermissions(version));
  const target = query.get('chunk');
  const targetPage = Number(query.get('page'));
  const pageChunk = !data.chunks.some(chunk => chunk.chunk_id === target) && targetPage > 0
    ? data.chunks.find(chunk => chunk.page_start != null && chunk.page_start <= targetPage && (chunk.page_end ?? chunk.page_start) >= targetPage) : null;
  const chunks = el('div', {className: 'chunk-list'});
  if (!data.chunks.length) chunks.append(empty('暂无可查看的分块', '此文档尚未发布可检索内容。'));
  data.chunks.forEach((chunk, i) => {
    const selected = chunk.chunk_id === target || chunk === pageChunk;
    const article = el('article', {className: `chunk-card${selected ? ' highlighted' : ''}`, id: `chunk-${chunk.chunk_id}`, tabindex: -1},
      el('div', {className: 'chunk-heading'}, el('strong', {}, `分块 ${String(i + 1).padStart(2, '0')}`),
        el('span', {className: 'badge'}, chunk.meta?.kind === 'table' ? '表格' : '正文'),
        el('span', {className: 'muted'}, pages(chunk.page_start, chunk.page_end)),
        selected && el('span', {className: 'located-tag'}, '引用所在分块')),
      el('div', {className: 'chunk-section'}, chunk.section_path || '未标注章节'), el('pre', {className: 'chunk-text'}, chunk.text));
    chunks.append(article);
  });
  page.append(el('div', {className: 'panel-heading outside'}, el('h2', {}, '文档分块'),
    el('span', {className: 'muted'}, `共 ${data.chunks.length} 个 · 原文按转换结果展示`)), chunks);
  return page;
}

export async function reviewView() {
  const data = await api('/api/review');
  const titles = await referenceTitles({pending: data.links}, [...data.documents, ...data.events]);
  const page = el('section', {}, heading('审核中心', '查看质检问题、未收录提问，以及需要人工核对的实体与事件。'));
  const body = el('div', {className: 'review-body'});
  const docsButton = el('button', {role: 'tab', type: 'button'}, `质检与异常文档 ${data.documents.length}`);
  const questionsButton = el('button', {role: 'tab', type: 'button'}, `未收录问题 ${data.unanswered.length}`);
  const entitiesButton = el('button', {role: 'tab', type: 'button'}, `实体待确认 ${data.entities.length}`);
  const eventsButton = el('button', {role: 'tab', type: 'button'}, `待审事件 ${data.events.length}`);
  const linksButton = el('button', {role: 'tab', type: 'button'}, '待确认引用 ' + data.links_total);
  const conflictsButton = el('button', {role: 'tab', type: 'button'}, '文档冲突 ' + data.conflicts.length);
  const recentButton = el('button', {role: 'tab', type: 'button'}, '最近处理');
  const versionsButton = el('button', {role: 'tab', type: 'button'}, '版本待确认 ' + data.versions_total);
  let tabRevision = 0;
  let documentControls = [];
  function paint(tab) {
    const revision = ++tabRevision;
    reviewTab = tab;
    for (const [key, button] of [['documents', docsButton], ['versions', versionsButton], ['questions', questionsButton], ['entities', entitiesButton], ['events', eventsButton], ['links', linksButton], ['conflicts', conflictsButton], ['recent', recentButton]]) {
      button.className = tab === key ? 'tab active' : 'tab';
      button.setAttribute('aria-selected', String(tab === key));
    }
    body.replaceChildren();
    documentControls.forEach(control => control.dispose());
    documentControls = [];
    if (tab === 'documents') {
      if (!data.documents.length) body.append(empty('暂无待复核文档', '当前文档没有记录质检警告或入库异常。'));
      else body.append(...data.documents.map(record => {
        const controls = documentReviewControls(record);
        documentControls.push(controls);
        return el('article', {className: 'panel review-document'},
        el('div', {className: 'panel-heading'}, el('div', {}, el('a', {href: docUrl(record.version_id)}, record.title),
          el('small', {className: 'muted'}, record.filename)), badge(record.status)),
        record.error || record.detail ? el('p', {className: 'review-reason'}, record.error || record.detail) : null, qcList(record), controls);
      }));
    } else if (tab === 'versions') {
      const versions = versionReview(data.versions, data.versions_total);
      documentControls.push(versions); body.append(versions); versions.mount();
    } else if (tab === 'entities') body.append(entityReview(data.entities));
    else if (tab === 'events') body.append(eventReview(data.events));
    else if (tab === 'links') {
      if (data.links_total > data.links.length) body.append(el('p', {className: 'muted'}, '只显示前 200 条，处理后刷新可看到后续'));
      body.append(linkReview(data.links, titles));
    }
    else if (tab === 'conflicts') body.append(conflictReview(data.conflicts));
    else if (tab === 'recent') {
      body.append(el('div', {className: 'loading'}, '正在加载最近处理…'));
      api('/api/review/decisions').then(result => {
        if (revision !== tabRevision) return;
        body.replaceChildren(result.decisions.length ? el('div', {className: 'panel'}, table(['时间', '类型', '决定', '操作'],
          result.decisions.map(row => [timeText(row.created_at), ({relation_reject: '否决关系', entity_subtype: '修改子类', series_create: '新建系列', series_rename: '系列改名', series_merge: '合并系列', document_series: '文档归属'})[row.kind] || '审核决定', row.label, undoControl(row)])))
          : empty('暂无最近处理', '人工审核决定将显示在这里，可随时撤销。'));
      }).catch(error => {
        if (revision === tabRevision) body.replaceChildren(el('div', {className: 'error-banner', role: 'alert'}, error.message),
          el('button', {className: 'button', onclick: () => paint('recent')}, '重试'));
      });
    }
    else {
      body.append(data.unanswered.length ? el('div', {className: 'panel'}, table(['记录时间', '问题', '状态', '操作'],
        data.unanswered.map(row => [timeText(row.time), el('span', {className: 'question-cell'}, row.question), badge(row.status),
          reviewControls([{text: '已处理', kind: 'question_done', target: row.id}])])))
        : empty('暂无未收录问题', '资料不足或没有检索命中的问题会记录在这里。'));
    }
  }
  docsButton.addEventListener('click', () => paint('documents'));
  versionsButton.addEventListener('click', () => paint('versions'));
  questionsButton.addEventListener('click', () => paint('questions'));
  entitiesButton.addEventListener('click', () => paint('entities'));
  eventsButton.addEventListener('click', () => paint('events'));
  linksButton.addEventListener('click', () => paint('links'));
  conflictsButton.addEventListener('click', () => paint('conflicts'));
  recentButton.addEventListener('click', () => paint('recent'));
  page.append(el('div', {className: 'tabs', role: 'tablist', 'aria-label': '审核内容'}, docsButton, versionsButton, questionsButton, entitiesButton, eventsButton, linksButton, conflictsButton, recentButton), body);
  paint(reviewTab);
  page.dispose = () => { tabRevision++; documentControls.forEach(control => control.dispose()); };
  return page;
}

export async function searchView(query) {
  const text = (query.get('q') || '').trim();
  const page = el('section', {}, heading('全局搜索', text ? `搜索“${text}”` : '在知识库的已发布内容中查找。'));
  if (!text) { page.append(empty('输入关键词开始搜索', '使用顶栏搜索框查找资料。')); return page; }
  const {hits} = await post('/api/search', {query: text});
  page.append(el('div', {className: 'result-count'}, `找到 ${hits.length} 条相关内容`));
  if (!hits.length) page.append(empty('暂无相关内容', '试试更具体的关键词，或先导入相关资料。'));
  else page.append(el('div', {className: 'search-results'}, hits.map(hit => el('article', {className: 'panel search-result'},
    el('div', {className: 'search-result-top'}, el('span', {className: 'rank'}, String(hit.rank).padStart(2, '0')), sourceLink(hit),
      el('span', {className: 'search-score'}, `匹配分数 ${Number(hit.score).toFixed(3)}`)),
    el('div', {className: 'citation-meta'}, hit.filename || '未知文件', ' · ', pages(hit.page_start, hit.page_end), ' · ', hit.section || '未标注章节'),
    el('p', {className: 'search-snippet'}, hit.text), el('a', {href: docUrl(hit.version_id, hit.chunk_id), className: 'text-link'}, '查看文档 →')))));
  return page;
}

export function placeholderView(kind) {
  const graph = kind === 'graph';
  return el('section', {}, heading(graph ? '知识图谱' : '时间轴', '逐步连接资料中的业务知识。'),
    el('div', {className: 'placeholder panel'}, el('div', {className: 'placeholder-art', 'aria-hidden': 'true'}, graph ? '◇ — ◇ — ◇' : '○ ─── ○ ─── ○'),
      el('span', {className: 'badge status-insufficient'}, '规划中'),
      el('h2', {}, graph ? '知识图谱' : '业务时间轴'),
      el('p', {className: 'muted'}, graph ? '规划中：知识图谱（人物、组织、事件关系，力导向图）' : '规划中：时间轴（业务事件）'),
      el('a', {href: '#/documents', className: 'button'}, '先去查看文档库')));
}
