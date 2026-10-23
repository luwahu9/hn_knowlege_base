import {el, api, heading, empty, table, docUrl, timeText} from './dom.js';
import {can, identityEpoch} from './auth_state.js';
import {refreshReview} from './review.js';
import {casefold} from './series_target.js';

// 空系列不在查询结果中；仅保留本次会话刚创建的目标，便于立即归入文档。
const created = new Map();
window.addEventListener('kb:identity-changed', () => created.clear());
window.addEventListener('kb:decision-removed', event => {
  if (event.detail.kind === 'series_create') created.delete(event.detail.target);
});
export const seriesUrl = id => '#/series/' + encodeURIComponent(id);
const errorText = error => error.status === 404 ? '对象不存在或无权查看' : error.message;
export const seriesTarget = row => `${row.folder || ''}/${casefold(row.filename.normalize('NFC'))}`;
export const periodText = period => !period?.start ? '时间未标注' : period.start === period.end || !period.end
  ? period.start : `${period.start} 至 ${period.end}`;

export function libraryTabs(series = false) {
  return el('div', {className: 'tabs library-tabs', 'aria-label': '文档库视图'},
    el('a', {href: '#/documents', className: 'tab' + (!series ? ' active' : ''), 'aria-current': !series ? 'page' : null}, '文档'),
    el('a', {href: '#/documents?view=series', className: 'tab' + (series ? ' active' : ''), 'aria-current': series ? 'page' : null}, '系列'));
}

// 所有操作共用同一生命周期；离开页面或换账号时停止后续批量请求并关闭表单。
function manager() {
  const epoch = identityEpoch(), hash = location.hash, controller = new AbortController();
  let disposed = false, dialog = null;
  const current = () => !disposed && epoch === identityEpoch() && hash === location.hash;
  const submit = (kind, target, value) => api('/api/review/decisions', {method: 'POST', signal: controller.signal,
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({kind, target, value})});
  function open(title, fields, handle, {saved = refreshReview} = {}) {
    if (!current()) return;
    dialog?.remove();
    const error = el('p', {className: 'error-text', role: 'alert'});
    const save = el('button', {type: 'submit', className: 'button primary'}, '保存');
    const cancel = el('button', {type: 'button', className: 'button', onclick: () => dialog.close()}, '取消');
    const form = el('form', {}, el('h2', {}, title), fields, error, el('div', {className: 'dialog-actions'}, cancel, save));
    let busy = false;
    dialog = el('dialog', {className: 'series-dialog', 'aria-label': title}, form);
    dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
    dialog.addEventListener('close', () => dialog?.remove());
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (busy || !current()) return;
      busy = true; save.disabled = cancel.disabled = true; error.textContent = ''; save.textContent = '正在保存…';
      try {
        const result = await handle(submit);
        if (!current()) return;
        dialog.close(); saved(result);
      } catch (failure) { if (current()) error.textContent = errorText(failure); }
      finally { busy = false; save.disabled = cancel.disabled = false; save.textContent = '保存'; }
    });
    document.body.append(dialog); dialog.showModal();
    form.querySelector('input,select')?.focus();
  }
  function newSeries(saved = refreshReview) {
    const name = el('input', {name: 'series_name', 'aria-label': '系列名称', autocomplete: 'off'});
    open('新建系列', el('label', {}, '系列名称', name), async submit => {
      const {decision} = await submit('series_create', 'new', name.value);
      created.set(decision.target, {id: decision.target, name: decision.value, auto: false});
      return decision;
    }, {saved});
  }
  return {open, newSeries, submit, current,
    dispose() { disposed = true; controller.abort(); dialog?.close(); dialog?.remove(); }};
}

export function createSeriesButton() {
  const m = manager();
  const button = el('button', {className: 'button primary', type: 'button', hidden: !can('review'), onclick: () => m.newSeries()}, '新建系列');
  button.dispose = m.dispose;
  return button;
}

export async function seriesListView(query) {
  const q = query.get('q') || '';
  const {series} = await api('/api/series?' + new URLSearchParams({q}));
  const create = createSeriesButton();
  const page = el('section', {}, heading('文档系列', '按期次浏览同一出版物，也可由审核员整理文档归属。', create), libraryTabs(true));
  const input = el('input', {type: 'search', name: 'series_q', value: q, placeholder: '搜索系列名称', 'aria-label': '搜索系列名称'});
  page.append(el('form', {className: 'filter-bar', onsubmit: event => {
    event.preventDefault(); location.hash = '#/documents?' + new URLSearchParams({view: 'series', q: input.value});
  }}, el('div', {className: 'filters'}, input, el('button', {className: 'button'}, '查找')),
    el('span', {className: 'muted'}, `共 ${series.length} 个系列`)),
    series.length ? el('div', {className: 'panel'}, table(['系列', '期数', '首期', '末期', '来源'], series.map(row => [
      el('a', {href: seriesUrl(row.id)}, row.name), row.count, periodText(row.first?.period), periodText(row.last?.period),
      row.auto ? row.folder || '根目录' : '人工',
    ]))) : empty('暂无符合条件的系列', '自动系列至少包含三份已发布文档；新建的人工系列归入文档后会显示在这里。'));
  page.dispose = create.dispose;
  return page;
}

export async function seriesSelection() {
  const m = manager(), selected = new Map();
  const root = el('div', {className: 'panel series-selection', hidden: !can('review')});
  if (!can('review')) return {element: root, checkbox: () => null, dispose: m.dispose};
  const {series} = await api('/api/series');
  const choices = new Map(series.map(s => [s.id, s]));
  for (const [id, row] of created) if (!choices.has(id)) choices.set(id, row);
  const select = el('select', {name: 'series_target', 'aria-label': '目标系列'});
  const label = row => `${row.name} · ${row.auto ? row.folder || '根目录' : '人工'}`;
  function options(value = select.value) {
    select.replaceChildren(el('option', {value: ''}, '请选择系列'), ...[...choices.values()].map(row => el('option', {value: row.id}, label(row))));
    select.value = value;
  }
  options();
  const count = el('strong', {'aria-live': 'polite'}, '已选 0 份文档');
  const results = el('div', {className: 'series-results', role: 'status', 'aria-live': 'polite'});
  const assign = el('button', {className: 'button primary', type: 'button', disabled: true}, '归入系列');
  const remove = el('button', {className: 'button', type: 'button', disabled: true}, '移出系列');
  const create = el('button', {className: 'button', type: 'button', onclick: () => m.newSeries(decision => {
    const row = created.get(decision.target); choices.set(row.id, row); options(row.id);
    results.replaceChildren(el('p', {}, '已新建：', row.name, '。请选择文档并归入。'));
  })}, '新建系列');
  let busy = false;
  const boxes = new Map();
  function update() {
    count.textContent = `已选 ${selected.size} 份文档`;
    assign.disabled = remove.disabled = busy || !selected.size; select.disabled = create.disabled = busy;
    for (const entries of boxes.values()) entries.forEach(box => { box.disabled = busy; });
  }
  function checkbox(row) {
    if (row.status && row.status !== 'published') return el('span', {className: 'muted'}, '—');
    const target = seriesTarget(row);
    const box = el('input', {type: 'checkbox', name: 'series_document', 'aria-label': `选择文档：${row.title || row.filename}`});
    if (!boxes.has(target)) boxes.set(target, []);
    boxes.get(target).push(box);
    box.addEventListener('change', () => {
      if (box.checked) selected.set(target, row); else selected.delete(target);
      boxes.get(target).forEach(item => { item.checked = box.checked; }); update();
    });
    return box;
  }
  async function apply(value) {
    if (busy || !selected.size || !m.current()) return;
    if (value === null) { results.replaceChildren(el('p', {className: 'error-text'}, '请选择系列')); return; }
    busy = true; update(); results.textContent = '正在逐份保存…';
    const failures = [], completed = [];
    for (const [target, row] of [...selected]) {
      if (!m.current()) break;
      try {
        await m.submit('document_series', target, value);
        created.delete(value);
        completed.push(row); selected.delete(target); boxes.get(target)?.forEach(box => { box.checked = false; });
      } catch (failure) {
        if (!m.current()) break;
        failures.push({row, message: errorText(failure)});
      }
    }
    busy = false; update();
    if (!m.current()) return;
    results.replaceChildren(el('p', {}, `已保存 ${completed.length} 份，失败 ${failures.length} 份。`),
      failures.length ? el('ul', {}, failures.map(({row, message}) => el('li', {className: 'error-text'}, row.title || row.filename, '：', message))) : null,
      el('button', {className: 'button', type: 'button', onclick: refreshReview}, '刷新查看结果'));
  }
  assign.addEventListener('click', () => apply(select.value || null));
  remove.addEventListener('click', () => apply(''));
  root.append(el('div', {className: 'series-selection-toolbar'}, count, select, assign, remove, create),
    el('p', {className: 'muted'}, '勾选本页已发布文档后批量整理；同一文档的各版本只提交一次。'), results);
  return {element: root, checkbox, dispose: m.dispose};
}

export function documentSeries(series) {
  if (!series) return null;
  const link = (row, direction) => row ? el('a', {href: docUrl(row.version_id), className: 'series-neighbor'},
    el('strong', {}, direction), row.title, el('small', {className: 'muted'}, periodText(row.period)))
    : el('span', {className: 'muted series-neighbor'}, `没有${direction}`);
  return el('section', {className: 'panel document-series'},
    el('div', {className: 'panel-heading'}, el('h2', {}, el('a', {href: seriesUrl(series.id)}, series.name)),
      el('strong', {}, `第 ${series.position} 期 / 共 ${series.total} 期`)),
    el('div', {className: 'series-neighbors'}, link(series.prev, '上一期'), link(series.next, '下一期')));
}

export async function seriesDetailView(id) {
  const data = await api('/api/series/' + encodeURIComponent(id));
  const series = data.series, m = manager(), selection = await seriesSelection();
  const actions = el('div', {className: 'review-actions'});
  if (can('review')) {
    actions.append(el('button', {className: 'button', type: 'button', onclick: () => {
      const input = el('input', {name: 'series_name', 'aria-label': '系列名称', value: series.name});
      m.open('系列改名', el('label', {}, '系列名称', input), submit => submit('series_rename', series.id, input.value));
    }}, '改名'));
    const mergeError = el('span', {className: 'error-text', role: 'alert'});
    const merge = el('button', {className: 'button', type: 'button', onclick: async () => {
      merge.disabled = true; mergeError.textContent = '';
      try {
        const {series: rows} = await api('/api/series');
        if (!m.current()) return;
        const select = el('select', {name: 'merge_target', 'aria-label': '并入的目标系列'},
          el('option', {value: ''}, '请选择目标系列'), rows.filter(row => row.id !== series.id).map(row =>
            el('option', {value: row.id}, `${row.name} · ${row.auto ? row.folder || '根目录' : '人工'}`)));
        m.open('合并系列', el('div', {}, el('p', {}, '将“', series.name, '”的全部成员并入：'), select),
          submit => submit('series_merge', series.id, select.value));
      } catch (failure) { if (m.current()) mergeError.textContent = errorText(failure); }
      finally { merge.disabled = false; }
    }}, '合并到其他系列');
    actions.append(merge, mergeError);
  }
  const page = el('section', {}, el('a', {href: '#/documents?view=series', className: 'back-link'}, '← 返回系列列表'),
    heading(series.name, `${series.count} 期 · ${series.auto ? series.folder || '根目录' : '人工系列'}`, actions),
    data.redirected_from ? el('p', {className: 'warning-banner'}, '该系列已并入 ', series.name) : null,
    el('div', {className: 'series-view-links'},
      can('knowledge') ? el('a', {className: 'button', href: '#/timeline?' + new URLSearchParams({series: series.id})}, '在时间轴中查看') : null,
      can('knowledge') ? el('a', {className: 'button', href: '#/graph?' + new URLSearchParams({series: series.id})}, '在图谱中查看') : null),
    selection.element,
    el('div', {className: 'panel'}, table([...(can('review') ? ['选择'] : []), '期次', '标题', '时间段', '导入时间'],
      data.members.map(row => [...(can('review') ? [selection.checkbox(row)] : []), `第 ${row.position} 期`,
        el('a', {href: docUrl(row.version_id)}, row.title || row.filename), periodText(row.period), timeText(row.imported_at)]))));
  page.dispose = () => { m.dispose(); selection.dispose(); };
  return page;
}

export async function seriesFilter(kind, query) {
  const id = query.get('series') || '';
  const {series} = await api('/api/series');
  let selected = series.find(row => row.id === id);
  if (id && !selected) selected = (await api('/api/series/' + encodeURIComponent(id))).series;
  const select = el('select', {name: 'series', 'aria-label': '按系列筛选'}, el('option', {value: ''}, '全部系列'),
    series.map(row => el('option', {value: row.id}, `${row.name} · ${row.auto ? row.folder || '根目录' : '人工'}`)));
  if (id && !series.some(row => row.id === id)) select.append(el('option', {value: id}, selected.name));
  select.value = id;
  const navigate = value => {
    const params = new URLSearchParams(query);
    if (value) params.set('series', value); else params.delete('series');
    params.delete('focus'); params.delete('depth');
    location.hash = '#/' + kind + (params.size ? '?' + params : '');
  };
  select.addEventListener('change', () => navigate(select.value));
  return el('div', {className: 'series-filter'}, el('label', {}, '系列 ', select),
    id ? el('strong', {className: 'series-active-filter'}, '仅显示系列：', selected.name) : null,
    id ? el('button', {className: 'button', type: 'button', onclick: () => navigate('')}, '清除系列筛选') : null);
}
