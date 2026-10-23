import {el, api, post, timeText, docUrl, table} from './dom.js';

const outcomes = {new: '新增', changed: '已改动', unchanged: '未改动', duplicate: '重复', rejected: '被拒',
  failed: '失败', skipped: '已跳过', missing: '缺失'};
const reasons = {invalid_path: '路径无效', hidden: '隐藏或临时文件', unsupported: '格式不支持', empty: '空文件', too_large: '超过大小上限'};

function countsText(counts = {}) {
  return Object.entries(outcomes).map(([key, text]) => `${text} ${counts[key] || 0}`).join(' · ');
}

export async function scanPanel() {
  const initial = await api('/api/scan');
  const element = el('section', {className: 'panel scan-panel'});
  if (!initial.roots.length) return {element, mount() {}, dispose() {}};
  const cards = el('div', {className: 'scan-roots'});
  const errorBox = el('p', {className: 'error-text', role: 'alert'});
  const controller = new AbortController();
  let active = false, disposed = false, timer = null, refreshing = false, status = initial;
  let needsStatus = false, statusRevision = 0;
  let reportDialog = null, reportRevision = 0;
  function paint() {
    cards.replaceChildren(...status.roots.map(root => {
      const button = el('button', {className: 'button primary', disabled: Boolean(root.active), onclick: async () => {
        button.disabled = true;
        try { await post('/api/scan', {root: root.name}); errorBox.textContent = ''; }
        catch (error) { if (!active) return; errorBox.textContent = error.status === 409 ? '正在扫描' : error.message; }
        needsStatus = true; statusRevision++;
        await refresh();
      }}, root.active ? '正在扫描' : '立即扫描');
      return el('article', {className: 'scan-root'}, el('div', {className: 'panel-heading'},
        el('div', {}, el('h3', {}, root.name), el('p', {className: 'muted scan-path'}, root.path)),
        el('div', {className: 'scan-actions'}, button,
          el('button', {className: 'button', onclick: () => showReports(root.name)}, '查看报告'))),
        root.active ? el('p', {className: 'scan-progress'}, root.active.status === 'queued' ? '排队中' : '扫描进行中',
          ' · ', root.active.progress || '等待进度') : null,
        root.last_run ? el('div', {}, el('small', {className: 'muted'}, `最近扫描：${timeText(root.last_run.finished_at)}`),
          el('p', {className: 'scan-counts'}, countsText(root.last_run.counts))) : el('p', {className: 'muted'}, '尚无扫描报告'));
    }));
  }
  function schedule() {
    clearTimeout(timer);
    if (active && (needsStatus || status.roots.some(root => root.active))) timer = setTimeout(refresh, 3000);
  }
  async function refresh() {
    if (!active || refreshing) return;
    refreshing = true;
    const revision = statusRevision;
    try {
      const result = await api('/api/scan', {signal: controller.signal});
      if (!active) return;
      status = result;
      if (revision === statusRevision) needsStatus = false;
      paint();
    } catch (error) { if (active) errorBox.textContent = error.message; }
    finally { refreshing = false; schedule(); }
  }
  async function showReports(root) {
    reportDialog?.remove();
    const revision = ++reportRevision;
    const list = el('div', {className: 'scan-run-list'});
    const details = el('div', {className: 'scan-report-details'}, el('p', {className: 'muted'}, '选择一份报告查看条目。'));
    const message = el('p', {className: 'error-text', role: 'alert'});
    const dialog = el('dialog', {className: 'scan-report-dialog', 'aria-label': `${root} 的扫描报告`},
      el('div', {className: 'dialog-heading'}, el('h2', {}, `${root} · 扫描报告`),
        el('button', {className: 'icon-button', 'aria-label': '关闭扫描报告', onclick: () => dialog.close()}, '×')),
      message, list, details);
    reportDialog = dialog;
    document.body.append(dialog); dialog.showModal();
    let detailRevision = 0;
    async function showReport(id) {
      const current = ++detailRevision;
      details.replaceChildren(el('p', {className: 'muted'}, '正在加载报告…'));
      try {
        const report = await api(`/api/scan/runs/${encodeURIComponent(id)}`, {signal: controller.signal});
        if (!active || revision !== reportRevision || current !== detailRevision) return;
        const filter = el('select', {'aria-label': '按扫描结果筛选'}, el('option', {value: ''}, '默认：不含跳过项'),
          el('option', {value: 'all'}, '全部条目'),
          Object.entries(outcomes).map(([key, label]) => el('option', {value: key}, label)));
        const entries = el('div', {className: 'scan-report-items'});
        function paintItems() {
          const rows = report.items.filter(item => filter.value === 'all' ||
            (filter.value ? item.outcome === filter.value : item.outcome !== 'skipped'));
          entries.replaceChildren(el('p', {className: 'muted'}, `当前 ${rows.length} 条`),
            rows.length ? table(['路径', '结果', '版本', '原因'], rows.map(item => [
              el('span', {className: 'scan-path'}, item.path), el('div', {}, outcomes[item.outcome] || item.outcome,
                item.outcome === 'missing' ? el('span', {className: `badge ${item.withdrawn ? 'status-withdrawn' : ''}`}, item.withdrawn ? '已撤下' : '未撤下') : null,
                item.outcome === 'duplicate' ? el('span', {className: `badge ${item.restored ? 'status-published' : ''}`}, item.restored ? '已恢复' : '未恢复') : null),
              el('div', {}, item.version_id ? el('a', {href: docUrl(item.version_id), onclick: () => dialog.close()}, item.version_id) : '—',
                item.previous_version ? el('small', {}, '旧版本 ', el('a', {href: docUrl(item.previous_version), onclick: () => dialog.close()}, item.previous_version)) : null),
              item.error || reasons[item.reason] || item.reason || '—'])) : el('p', {className: 'muted'}, '没有此类条目。'));
        }
        filter.addEventListener('change', paintItems);
        const skipped = report.items.filter(item => item.outcome === 'skipped');
        details.replaceChildren(...[el('h3', {}, report.scan_id), el('p', {className: 'muted'},
          `${timeText(report.started_at)} — ${timeText(report.finished_at)}`), el('p', {className: 'scan-counts'}, countsText(report.counts)),
          report.truncated ? el('p', {className: 'warning-banner'}, '仅显示前 2000 条') : null, filter, entries,
          skipped.length ? el('details', {className: 'scan-skipped'}, el('summary', {}, `已跳过 ${skipped.length} 条（展开查看）`),
            el('ul', {}, skipped.map(item => el('li', {}, item.path, ' · ', reasons[item.reason] || item.reason)))) : null].filter(Boolean));
        paintItems();
      } catch (error) { if (active && revision === reportRevision && current === detailRevision) details.replaceChildren(el('p', {className: 'error-text'}, error.message)); }
    }
    try {
      const {runs} = await api(`/api/scan/runs?root=${encodeURIComponent(root)}&limit=100`, {signal: controller.signal});
      if (!active || revision !== reportRevision) return;
      list.replaceChildren(...runs.map(run => el('button', {className: 'button', onclick: () => showReport(run.scan_id)},
        `${timeText(run.started_at)} · ${run.scan_id}`)));
      if (!runs.length) list.append(el('p', {className: 'muted'}, '尚无扫描报告。'));
      else await showReport(runs[0].scan_id);
    } catch (error) { if (active && revision === reportRevision) message.textContent = error.message; }
  }
  element.append(el('div', {className: 'panel-heading'}, el('h2', {}, '服务器目录'),
    el('small', {className: 'muted'}, initial.interval_minutes ? `每 ${initial.interval_minutes} 分钟自动扫描` : '手动扫描')),
    cards, errorBox);
  paint();
  return {element, mount() { if (!disposed) { active = true; schedule(); } }, dispose() {
    disposed = true; active = false; clearTimeout(timer); controller.abort(); reportRevision++;
    reportDialog?.close(); reportDialog?.remove();
  }};
}
