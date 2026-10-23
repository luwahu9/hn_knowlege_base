import {seriesFilter} from './series.js';
export {graphView} from './entity_graph.js';
import {evidence} from './evidence.js';
import {reviewControls, eventControls, refreshReview} from './review.js';
import {el, api, post, empty, heading, pages, docUrl, table} from './dom.js';
import {can} from './auth_state.js';
import {entityType, entitySubtype} from './entity_types.js';

const precision = {day: '日', month: '月', quarter: '季度', half_year: '半年', year: '年'};
const verdicts = {pass: '通过', review: '待审', reject: '已丢弃'};
const problems = {quote_not_found: '引文未找到', subject_missing: '缺少主体', action_missing: '缺少行为',
  subject_not_in_quote: '主体不在引文中', object_not_in_quote: '对象不在引文中', time_not_in_quote: '时间不在引文中',
  place_not_in_quote: '地点不在引文中', time_missing: '缺少时间', date_unresolved: '日期无法确定', quote_ambiguous: '引文多处出现'};
let timelineReview = true;
let timelineStart = '', timelineEnd = '';

function timelineQuery(series = '') {
  const params = new URLSearchParams({review: timelineReview ? 1 : 0});
  if (series) params.set('series', series);
  if (timelineStart) params.set('start', timelineStart);
  if (timelineEnd) params.set('end', timelineEnd);
  return params;
}

export function graphUrl(id, depth = 1) {
  return `#/graph?${new URLSearchParams({focus: id, depth})}`;
}

function issueText(event) {
  return event.problems.map(problem => problems[problem] || problem).join('、') || '校验通过';
}

function eventDetail(event, onChanged = refreshReview) {
  const fields = [['主体', event.subject], ['行为', event.action], ['对象', event.object], ['地点', event.place],
    ['原文时间', event.time_expr], ['规范化时间', event.start ? `${event.start} 至 ${event.end}（${precision[event.precision] || '未知精度'}）` : '未能确定']];
  return el('article', {className: 'knowledge-detail'},
    el('span', {className: `badge kg-${event.verdict}`}, verdicts[event.verdict]), el('h2', {}, event.label),
    el('dl', {}, fields.map(([label, value]) => el('div', {}, el('dt', {}, label), el('dd', {}, value || '未标注')))),
    el('h3', {}, '证据引文'), evidence(event),
    el('p', {className: event.problems.length ? 'kg-problems' : 'muted'}, issueText(event)),
    eventControls(event, onChanged));
}

export function knowledgeCard(data) {
  return el('a', {href: '#/timeline', className: 'panel knowledge-summary'},
    el('div', {}, el('h2', {}, '知识抽取'), el('p', {className: 'muted'}, `已抽取 ${data.extracted} / ${data.published} 份已发布文档`)),
    el('div', {className: 'knowledge-counts'}, el('span', {}, el('strong', {}, data.entities), ' 个实体'),
      el('span', {}, el('strong', {}, data.events.pass), ' 条通过事件'),
      el('span', {}, el('strong', {}, data.events.review), ' 条待审事件')), el('span', {}, '查看时间轴 →'));
}

export function entityReview(rows) {
  return rows.length ? el('div', {className: 'review-list'}, rows.map(row =>
    el('article', {className: 'panel review-entity', 'data-entity-id': row.id},
      el('h3', {}, el('a', {href: graphUrl(`ent:${row.id}`)}, row.canonical)),
      el('p', {className: 'entity-classification'}, el('span', {className: 'kind-' + entityType(row.type).key}, entityType(row.type).label),
        ' / ', entitySubtype(row.subtype)),
      el('div', {className: 'kg-link-list'}, row.candidates.map(candidate => el('div', {className: 'review-candidate'},
        el('a', {href: graphUrl(`ent:${candidate.id}`)}, candidate.canonical),
        reviewControls([{text: '合并到此', kind: 'entity_merge', target: row.id, value: candidate.id}])))),
      reviewControls([{text: '不是同一实体', kind: 'entity_distinct', target: row.id}]))))
    : empty('暂无待确认实体', '名称相似但不能自动合并的实体会显示在这里。');
}

export function eventReview(rows) {
  return rows.length ? el('div', {className: 'review-list'}, rows.map(row =>
    el('article', {className: 'panel review-event'},
      el('div', {className: 'review-card-top'}, el('div', {}, el('span', {className: 'badge kg-review'}, '待审事件'),
        el('h3', {}, row.label)), eventControls(row)),
      el('p', {className: 'muted'}, issueText(row)), evidence(row))))
    : empty('暂无待审事件', '时间缺失或证据有歧义的事件会显示在这里。');
}

export async function timelineView(query = new URLSearchParams()) {
  const series = query.get('series') || '';
  const seriesControl = await seriesFilter('timeline', query);
  let [status, data] = await Promise.all([api('/api/knowledge'), api('/api/timeline?' + timelineQuery(series))]);
  const titles = new Map([...data.items, ...data.undated].map(row => [row.version_id, row.title]));
  let instance = null, timer = null, disposed = false, refreshVersion = 0, timelineWidth = 0;
  let wasActive = !status.jobs.idle;
  const page = el('section', {className: 'knowledge-page'});
  const start = el('button', {className: 'button primary', type: 'button', hidden: !can('review')}, '抽取未处理文档');
  const summary = el('div', {className: 'kg-status', 'aria-live': 'polite'});
  const errors = el('div', {role: 'alert'});
  const failures = el('details', {className: 'kg-failures'});
  const checked = el('input', {name: 'review', type: 'checkbox', checked: timelineReview});
  const dateStart = el('input', {name: 'start', type: 'date', value: timelineStart, 'aria-label': '起始日期'});
  const dateEnd = el('input', {name: 'end', type: 'date', value: timelineEnd, 'aria-label': '结束日期'});
  const range = el('form', {className: 'kg-controls'}, el('label', {}, '起始日期 ', dateStart),
    el('label', {}, '结束日期 ', dateEnd), el('button', {className: 'button', type: 'submit'}, '筛选日期'));
  const truncated = el('p', {className: 'truncation-note', role: 'status', hidden: true});
  const visualization = el('div', {className: 'timeline-canvas', 'aria-label': '按文档分组的事件时间轴'});
  const detail = el('aside', {className: 'panel kg-side'}, empty('选择一个事件', '点击时间轴条目或下方事件查看证据。'));
  const undated = el('div', {className: 'undated-list'});
  const resizeTimeline = new ResizeObserver(() => {
    const width = visualization.querySelector('.vis-panel.vis-center')?.clientWidth || 0;
    if (instance && width > 0 && width !== timelineWidth) fitTimelineLabels();
  });
  page.append(heading('业务时间轴', '从原文中抽取事件，沿时间查看业务进展与证据。', start), seriesControl, summary, failures, errors,
    el('div', {className: 'kg-controls'}, el('label', {className: 'kg-check'}, checked, '显示待审事件')), range, truncated,
    el('div', {className: 'knowledge-layout'}, el('div', {className: 'panel kg-main'}, visualization), detail),
    el('div', {className: 'panel-heading outside'}, el('h2', {}, '未能确定时间的事件')), undated);

  function paintStatus() {
    summary.replaceChildren(el('strong', {}, `已抽取 ${status.extracted} / ${status.published} 份文档`),
      el('span', {}, `${status.entities} 个实体`), el('span', {}, `通过 ${status.events.pass} · 待审 ${status.events.review}`),
      el('span', {className: 'muted'}, status.jobs.idle ? '抽取队列空闲' :
        `排队 ${status.jobs.queued.length} · 正在处理：${titles.get(status.jobs.running) || status.jobs.running || '准备中'}`));
    start.disabled = !status.jobs.idle;
    failures.hidden = !status.jobs.failed.length;
    failures.replaceChildren(el('summary', {}, `${status.jobs.failed.length} 份文档抽取失败（展开查看）`),
      ...status.jobs.failed.map(row => el('p', {}, titles.get(row.version_id) || row.version_id, '：', row.error)));
  }

  function paintTimeline() {
    if (disposed) return;
    truncated.hidden = !data.truncated;
    truncated.textContent = data.truncated ? `仅显示最近 ${Math.max(data.items.length, data.undated.length)} 条，请按起止日期缩小范围。` : '';
    instance?.destroy();
    instance = null;
    visualization.replaceChildren();
    detail.replaceChildren(empty('选择一个事件', '点击时间轴条目或下方事件查看证据。'));
    if (data.items.length) {
      if (!window.vis?.Timeline || !window.vis?.DataSet) throw new Error('时间轴本地组件加载失败');
      const groups = [...new Map(data.items.map(row => [row.version_id, {id: row.version_id, content: el('span', {title: row.title}, row.title)}])).values()];
      const items = data.items.map(row => {
        const item = {id: row.id, group: row.version_id, content: el('span', {}, row.verdict === 'review' ? '待审 · ' : '', row.label),
          start: new Date(`${row.start}T00:00:00`), type: row.precision === 'day' ? 'point' : 'range',
          className: row.verdict === 'review' ? 'kg-timeline-review' : 'kg-timeline-pass'};
        if (item.type === 'range') {
          item.end = new Date(`${row.end}T00:00:00`);
          item.end.setDate(item.end.getDate() + 1);
        }
        return item;
      });
      instance = new window.vis.Timeline(visualization, new window.vis.DataSet(items), new window.vis.DataSet(groups),
        {locale: 'zh-cn', showCurrentTime: false, editable: false, maxHeight: 620, orientation: 'top',
          margin: {item: 12, axis: 14}, tooltip: {followMouse: false}});
      instance.on('select', ({items: selected}) => {
        const event = data.items.find(row => row.id === selected[0]);
        if (event) detail.replaceChildren(eventDetail(event));
      });
      fitTimelineLabels();
    } else visualization.append(empty('暂无可定位到时间轴的事件', '时间无法确定的事件列在下方。'),
      can('review') ? el('button', {className: 'button kg-empty-action', disabled: !status.jobs.idle, onclick: () => start.click()}, '抽取未处理文档') : null);
    undated.replaceChildren(...(data.undated.length ? data.undated.map(row => el('button',
      {type: 'button', className: 'panel undated-event', onclick: () => { detail.replaceChildren(eventDetail(row)); detail.scrollIntoView({block: 'nearest'}); }},
      el('span', {className: `badge kg-${row.verdict}`}, verdicts[row.verdict]), el('strong', {}, row.label),
      el('small', {className: 'muted'}, row.title, ' · ', issueText(row)))) : [empty('暂无未定时间事件')]));
  }

  function fitTimelineLabels() {
    // point 的组件宽度只计圆点，默认 fit 不会给右侧文字留出空间。
    const center = visualization.querySelector('.vis-panel.vis-center');
    timelineWidth = center?.clientWidth || visualization.clientWidth;
    if (!instance || timelineWidth <= 0) return;
    visualization.style.setProperty('--timeline-label-width', Math.max(40, Math.min(320, timelineWidth * .6)) + 'px');
    instance.redraw();
    instance.fit({animation: false});
    const labels = [...visualization.querySelectorAll('.vis-point .vis-item-content')];
    const labelWidth = Math.max(0, ...labels.map(node => node.getBoundingClientRect().width));
    const fraction = Math.min(.95, (labelWidth + 24) / timelineWidth);
    const viewWindow = instance.getWindow(), span = viewWindow.end.getTime() - viewWindow.start.getTime();
    instance.setWindow(viewWindow.start, new Date(viewWindow.end.getTime() + span * fraction / (1 - fraction)), {animation: false});
  }

  async function refresh() {
    const revision = ++refreshVersion;
    const result = await api('/api/timeline?' + timelineQuery(series));
    if (disposed || revision !== refreshVersion) return;
    data = result;
    paintTimeline();
  }

  function showError(error) { if (!disposed) errors.replaceChildren(el('div', {className: 'error-banner'}, error.message)); }
  async function poll() {
    clearTimeout(timer);
    try {
      const next = await api('/api/knowledge');
      if (disposed) return;
      status = next;
      paintStatus();
      if (wasActive && status.jobs.idle) await refresh();
      wasActive = !status.jobs.idle;
    } catch (error) { showError(error); }
    if (!disposed && (!status.jobs.idle || wasActive)) timer = setTimeout(poll, 2000);
  }
  start.addEventListener('click', async () => {
    start.disabled = true;
    errors.replaceChildren();
    try { await post('/api/knowledge/extract', {}); wasActive = true; await poll(); }
    catch (error) { showError(error); start.disabled = false; }
  });
  checked.addEventListener('change', async () => {
    timelineReview = checked.checked;
    try { await refresh(); } catch (error) { showError(error); }
  });
  range.addEventListener('submit', async event => {
    event.preventDefault();
    errors.replaceChildren();
    if (dateStart.value && dateEnd.value && dateStart.value > dateEnd.value) {
      showError(new Error('起始日期不能晚于结束日期'));
      return;
    }
    timelineStart = dateStart.value;
    timelineEnd = dateEnd.value;
    try { await refresh(); } catch (error) { showError(error); }
  });
  page.mount = () => { paintStatus(); paintTimeline(); resizeTimeline.observe(visualization); if (!status.jobs.idle) timer = setTimeout(poll, 2000); };
  page.dispose = () => { disposed = true; resizeTimeline.disconnect(); clearTimeout(timer); instance?.destroy(); instance = null; };
  return page;
}

