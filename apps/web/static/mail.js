import {el, api, post, empty, heading, table, timeText} from './dom.js';
import {can, getIdentity} from './auth_state.js';
import {mailQaPanel} from './mail_qa.js';
export {mailQaPanel} from './mail_qa.js';

const fields = ['q', 'sender', 'recipient', 'start', 'end', 'folder', 'attachments'];
const problems = {charset_fallback: '字符集声明缺失或有误，已尝试回退解码', header_decode: '头字段编码有问题，已尽量恢复',
  bad_date: '发送日期不合法', date_no_zone: '日期缺少时区，已按北京时间处理', no_body: '没有可用的正文',
  truncated: '正文、主题或列表已截断', defects: '邮件的 MIME 结构有问题'};
const outcomes = {dirs_listed: '列出目录', dirs_unchanged: '目录未变', dirs_removed: '目录移除', dirs_failed: '目录失败',
  dirs_skipped: '目录跳过', new: '新增', changed: '改动', unchanged: '未改动', removed: '删除', invalid: '不是邮件', failed: '失败', skipped: '跳过'};
const reasons = {invalid_path: '路径无效', hidden: '隐藏或临时文件', unsupported: '格式不支持', empty: '空文件', too_large: '超过大小上限'};
const address = value => value ? [value.name, value.addr && `<${value.addr}>`].filter(Boolean).join(' ') || '—' : '—';
const enabled = () => Boolean(getIdentity()?.features?.mail);

function navigate(params) {
  const hash = '#/mail?' + params.toString();
  if (location.hash === hash) window.dispatchEvent(new Event('hashchange'));
  else location.hash = hash;
}

function highlighted(value, terms) {
  const text = String(value || ''), result = el('span');
  // 先建文本节点；高亮也只用 mark 文本节点，不拼接 HTML。
  const folded = text.normalize('NFKC').toLowerCase();
  if (folded.length !== text.length || !terms.length) { result.append(text); return result; }
  const ranges = [];
  for (const term of terms) {
    const wanted = term.normalize('NFKC').toLowerCase();
    if (!wanted) continue;
    for (let start = folded.indexOf(wanted); start >= 0; start = folded.indexOf(wanted, start + wanted.length)) {
      ranges.push([start, start + wanted.length]);
    }
  }
  ranges.sort((a, b) => a[0] - b[0]);
  const merged = [];
  for (const range of ranges) {
    const last = merged.at(-1);
    if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1]);
    else merged.push([...range]);
  }
  let end = 0;
  for (const [first, last] of merged) { result.append(text.slice(end, first), el('mark', {}, text.slice(first, last))); end = last; }
  result.append(text.slice(end));
  return result;
}

export async function mailView(query = new URLSearchParams()) {
  const controls = {};
  const field = (name, label, type = 'text') => {
    const input = el('input', {name, type, value: query.get(name) || '', 'aria-label': label});
    controls[name] = input;
    return el('label', {}, label, input);
  };
  const keyword = field('q', '关键词');
  controls.q.placeholder = '关键词；双引号内的短语整体检索';
  const error = el('p', {className: 'error-text mail-error', role: 'alert'});
  const attachments = el('select', {name: 'attachments', 'aria-label': '附件'}, el('option', {value: ''}, '不限'),
    el('option', {value: '1'}, '有附件'), el('option', {value: '0'}, '无附件'));
  attachments.value = query.get('attachments') || ''; controls.attachments = attachments;
  const form = el('form', {className: 'panel mail-search-form'},
    el('div', {className: 'mail-search-main'}, keyword, el('button', {className: 'button primary', type: 'submit'}, '检索邮件')),
    error, el('div', {className: 'mail-filters'}, field('sender', '发件人（地址或名字）'), field('recipient', '收件人（地址或名字）'),
      field('start', '开始日期', 'date'), field('end', '结束日期', 'date'), field('folder', '文件夹'), el('label', {}, '附件', attachments)));
  const parameters = offset => {
    const params = new URLSearchParams();
    for (const key of fields) if (controls[key].value) params.set(key, controls[key].value);
    params.set('offset', String(offset)); params.set('limit', '20');
    return params;
  };
  form.addEventListener('submit', event => { event.preventDefault(); navigate(parameters(0)); });
  const page = el('section', {className: 'mail-page'}, heading('邮件', '按关键词、往来地址、日期和文件夹检索。附件仅收录名称，不收录内容。'), form);
  const qa = mailQaPanel({filters: () => Object.fromEntries(fields.filter(key => key !== 'q').map(key => [key, controls[key].value]))});
  page.append(qa.element);
  page.dispose = () => qa.dispose();
  const offset = Math.max(0, Number(query.get('offset')) || 0);
  const params = parameters(offset);
  let data;
  try { data = await api('/api/mail/search?' + params); }
  catch (failure) {
    if (failure.name === 'AbortError') throw failure;
    error.textContent = failure.message;
    return page;
  }
  const terms = [...(controls.q.value || '').matchAll(/"([^"]*)"|([^\s"]+)/g)].map(match => match[1] || match[2]);
  page.append(el('p', {className: 'muted mail-total'}, data.total_exact ? `共 ${data.total} 封` : '超过 10000 封'));
  page.append(data.rows.length ? el('div', {className: 'panel mail-results'}, table(
    ['主题', '发件人', '收件人', '发送时间', '文件夹', '附件', '摘要'], data.rows.map(row => [
      el('a', {href: '#/mail/' + encodeURIComponent(row.mail_id), className: 'mail-subject'}, highlighted(row.subject || '（无主题）', terms)),
      address(row.sender), el('span', {}, row.to.map(address).join('；') || '—', row.recipients > 5 ? ` 等 ${row.recipients} 人` : ''),
      row.sent_at ? timeText(row.sent_at) : '—', row.folder, String(row.attachments),
      el('div', {className: 'mail-snippet'}, highlighted(row.snippet, terms))]))) : empty('没有符合条件的邮件', '请调整关键词或筛选条件。'));
  const turn = next => {
    const preserved = new URLSearchParams(params); preserved.set('offset', String(next)); navigate(preserved);
  };
  page.append(el('div', {className: 'document-pagination'}, el('span', {className: 'muted'}, `第 ${data.total ? Math.floor(offset / 20) + 1 : 0} 页`),
    el('button', {className: 'button', type: 'button', disabled: offset === 0, onclick: () => turn(Math.max(0, offset - 20))}, '上一页'),
    el('button', {className: 'button', type: 'button', disabled: offset + 20 >= data.total || offset + 40 > 10000,
      onclick: () => turn(offset + 20)}, '下一页')));
  return page;
}

export async function mailDetailView(id) {
  const mail = await api('/api/mail/' + encodeURIComponent(id));
  const people = values => (values || []).map(address).join('；') || '—';
  const page = el('section', {className: 'mail-page'}, heading('邮件详情', mail.subject || '（无主题）',
    el('a', {href: '#/mail', className: 'button'}, '返回邮件检索')));
  page.append(el('section', {className: 'panel mail-headers'}, table(['字段', '内容'], [
    ['主题', mail.subject || '（无主题）'], ['发件人', address(mail.sender)], ['收件人', people(mail.to)],
    ['抄送', people(mail.cc)], ['密送', people(mail.bcc)], ['发送时间', mail.sent_at ? timeText(mail.sent_at) : '—'],
    ['文件夹', mail.folder], ['原件路径', mail.path], ['原件大小', `${mail.size} 字节`], ['Message-ID', mail.message_id || '—'],
    ['回复邮件', mail.in_reply_to || '—'], ['引用标识', (mail.references || []).join('；') || '—']]),
    el('a', {href: '/api/mail/' + encodeURIComponent(id) + '/raw', className: 'button primary', download: ''}, '下载原件')));
  if (mail.problems?.length) page.append(el('section', {className: 'panel'}, el('h2', {}, '解析提示'),
    el('ul', {}, mail.problems.map(code => el('li', {}, problems[code] || code)))));
  page.append(el('section', {className: 'panel'}, el('h2', {}, '正文'),
    mail.body_truncated ? el('p', {className: 'warning-banner'}, '正文超过上限，以下为截断内容。') : null,
    mail.body ? el('pre', {className: 'mail-body'}, mail.body) : el('p', {className: 'muted'}, '没有可用的正文')));
  page.append(el('section', {className: 'panel'}, el('h2', {}, `附件（${mail.attachments.length}）`),
    mail.attachments.length ? table(['名称', '类型', '大小'], mail.attachments.map(item => [item.name || '（无名称）', item.content_type, `${item.size} 字节`]))
      : el('p', {className: 'muted'}, '没有附件'), el('p', {className: 'muted'}, '这里只显示附件信息，不提供附件下载。')));
  const conversation = el('section', {className: 'panel mail-thread'}, el('h2', {}, '会话线索'));
  page.append(conversation);
  try {
    const thread = await api('/api/mail/' + encodeURIComponent(id) + '/thread');
    if (thread.root) conversation.append(el('p', {className: 'muted'}, '会话根：', thread.root));
    if (thread.truncated) conversation.append(el('p', {className: 'warning-banner'}, '只显示前 500 封'));
    conversation.append(el('ol', {className: 'mail-thread-list'}, thread.rows.map(row => el('li',
      {className: row.mail_id === thread.current ? 'mail-thread-current' : '', 'aria-current': row.mail_id === thread.current ? 'true' : null},
      el('a', {href: '#/mail/' + encodeURIComponent(row.mail_id)}, row.subject || '（无主题）'),
      row.mail_id === thread.current ? el('span', {className: 'badge'}, '当前邮件') : null,
      el('small', {}, row.sent_at ? timeText(row.sent_at) : '时间未标注', ' · ', address(row.sender), ' · ', row.folder),
      el('p', {className: 'mail-snippet'}, row.snippet)))));
  } catch (failure) {
    if (failure.name === 'AbortError') throw failure;
    conversation.append(el('p', {className: 'error-text', role: 'alert'}, '暂时无法加载会话线索：', failure.message));
  }
  return page;
}

export async function mailScanPanel() {
  const element = el('section', {className: 'panel scan-panel mail-scan-panel'});
  if (!enabled() || !can('scan')) { element.hidden = true; return {element, mount() {}, dispose() {}}; }
  let status = await api('/api/mail/scan');
  const controller = new AbortController(), cards = el('div', {className: 'scan-roots'});
  const error = el('p', {className: 'error-text', role: 'alert'});
  let active = false, disposed = false, busy = false, timer = null, pending = false, statusRevision = 0, generation = 0, dialog = null;
  const counts = value => Object.entries(outcomes).map(([key, text]) => `${text} ${value?.[key] || 0}`).join(' · ');
  const partProgress = report => report && Number.isInteger(report.parts) ? el('p', {className: 'mail-part-progress'},
    `分片 ${report.parts_done || 0}/${report.parts} · 失败分片 ${report.parts_failed || 0}`) : null;
  const finished = report => report.finished_at ? timeText(report.finished_at) : report.parts_failed ? '尚未完成（有失败分片）' : '进行中';
  function schedule() {
    clearTimeout(timer);
    if (active && (pending || status.roots.some(root => root.active))) timer = setTimeout(refresh, 3000);
  }
  async function refresh() {
    if (!active || busy) return;
    busy = true;
    const revision = statusRevision;
    try {
      const result = await api('/api/mail/scan', {signal: controller.signal});
      if (!active) return;
      status = result;
      if (revision === statusRevision) pending = false;
      paint();
    } catch (failure) { if (active) error.textContent = failure.message; }
    finally { busy = false; schedule(); }
  }
  async function showReports(root) {
    dialog?.close(); dialog?.remove();
    const current = ++generation;
    const list = el('div', {className: 'scan-run-list'}), details = el('div', {className: 'scan-report-details'});
    const modal = el('dialog', {className: 'scan-report-dialog', 'aria-label': `${root} 的邮件扫描报告`},
      el('div', {className: 'dialog-heading'}, el('h2', {}, root + ' · 邮件扫描报告'),
        el('button', {className: 'icon-button', 'aria-label': '关闭邮件扫描报告', onclick: () => modal.close()}, '×')), list, details);
    modal.addEventListener('close', () => { if (dialog === modal) { generation++; dialog = null; } modal.remove(); });
    dialog = modal; document.body.append(modal); modal.showModal();
    let selected = 0;
    async function showReport(id) {
      const revision = ++selected;
      details.replaceChildren(el('p', {className: 'muted'}, '正在加载报告…'));
      try {
        const report = await api('/api/mail/scan/runs/' + encodeURIComponent(id), {signal: controller.signal});
        if (!active || current !== generation || revision !== selected) return;
        const items = report.items.filter(item => ['failed', 'invalid', 'skipped', 'dirs_failed', 'dirs_skipped'].includes(item.outcome));
        details.replaceChildren(...[el('h3', {}, report.scan_id), el('p', {className: 'muted'},
          `${timeText(report.started_at)} — ${finished(report)}`), partProgress(report), el('p', {className: 'scan-counts'}, counts(report.counts)),
        report.truncated ? el('p', {className: 'warning-banner'}, '报告仅保留前 2000 个条目，以下为其中的问题与跳过项。') : null,
        items.length ? table(['路径', '结果', '原因'], items.map(item => [item.path || '根目录', outcomes[item.outcome] || item.outcome,
          item.error || reasons[item.reason] || item.reason || '—'])) : el('p', {className: 'muted'}, '当前报告条目中没有失败或跳过项。')].filter(Boolean));
      } catch (failure) { if (active && current === generation && revision === selected) details.replaceChildren(el('p', {className: 'error-text'}, failure.message)); }
    }
    try {
      const {runs} = await api('/api/mail/scan/runs?root=' + encodeURIComponent(root) + '&limit=20', {signal: controller.signal});
      if (!active || current !== generation) return;
      list.replaceChildren(...runs.map(run => el('button', {className: 'button', onclick: () => showReport(run.scan_id)}, timeText(run.started_at))));
      if (runs.length) await showReport(runs[0].scan_id);
      else details.replaceChildren(el('p', {className: 'muted'}, '尚无邮件扫描报告。'));
    } catch (failure) { if (active && current === generation) details.replaceChildren(el('p', {className: 'error-text'}, failure.message)); }
  }
  function paint() {
    cards.replaceChildren(...status.roots.map(root => {
      const trigger = (full, text) => el('button', {className: 'button', disabled: Boolean(root.active), onclick: async event => {
        event.target.disabled = true;
        error.textContent = '';
        try { await post('/api/mail/scan', {root: root.name, full}); }
        catch (failure) { if (active) error.textContent = failure.message; }
        pending = true; statusRevision++; await refresh();
      }}, text);
      return el('article', {className: 'scan-root'}, el('div', {className: 'panel-heading'},
        el('div', {}, el('h3', {}, root.name), el('p', {className: 'muted scan-path'}, root.path)),
        el('div', {className: 'scan-actions'}, trigger(false, '增量扫描'), trigger(true, '全量扫描'),
          el('button', {className: 'button', onclick: () => showReports(root.name)}, '最近报告'))),
        el('p', {className: 'muted'}, `目录 ${root.stats.dirs} · 邮件 ${root.stats.mails}`),
        root.active ? el('p', {className: 'scan-progress'}, root.active.status === 'queued' ? '排队中' : '扫描进行中',
          root.active.pending !== undefined ? ` · 待完成任务 ${root.active.pending}` : '', ' · ', root.active.progress || '等待进度') : null,
        root.last_run ? el('div', {}, el('small', {className: 'muted'}, '最近扫描：' + finished(root.last_run)), partProgress(root.last_run),
          el('p', {className: 'scan-counts'}, counts(root.last_run.counts))) : el('p', {className: 'muted'}, '尚无扫描报告'));
    }));
  }
  element.append(el('div', {className: 'panel-heading'}, el('h2', {}, '邮件扫描'), el('small', {className: 'muted'},
    status.interval_minutes ? `每 ${status.interval_minutes} 分钟自动扫描` : '手动扫描')), cards, error);
  if (!status.roots.length) cards.append(el('p', {className: 'muted'}, '没有配置邮件根目录。'));
  else paint();
  return {element, mount() { if (!disposed) { active = true; schedule(); } }, dispose() {
    disposed = true; active = false; generation++; clearTimeout(timer); controller.abort(); dialog?.close(); dialog?.remove();
  }};
}
