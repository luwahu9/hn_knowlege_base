import {requestOptions, unauthorized, identityEpoch, requireEpoch} from './auth_state.js';

// 所有外部文本都作为文本节点写入，不解析 HTML。
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key in node && !key.startsWith('aria')) node[key] = value;
    else if (value !== null && value !== undefined) node.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export async function api(path, options = {}) {
  const epoch = identityEpoch();
  let response;
  try { response = await fetch(path, requestOptions(options)); }
  catch (error) { if (error.name === 'AbortError') throw error; throw new Error('无法连接本机服务，请确认服务已启动后重试。'); }
  let data;
  try { data = await response.json(); }
  catch { requireEpoch(epoch); if (response.status === 401) unauthorized(); throw new Error('服务返回了无法读取的响应。'); }
  requireEpoch(epoch);
  if (!response.ok) {
    if (response.status === 401) unauthorized(data.error);
    const error = new Error(data.error || `请求失败（${response.status}）`);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

export function post(path, body) {
  return api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
}

export const labels = {
  published: '已发布', withdrawn: '已撤下', superseded: '已替代', purged: '已删除', unsupported: '格式不支持', qc_blocked: '质检阻断', no_chunks: '无可用分块', error: '执行异常',
  answered: '已回答', ungrounded: '无依据', insufficient: '资料不足', no_hits: '暂未收录', invalid_output: '输出异常',
};

export function badge(status) {
  const known = Object.hasOwn(labels, status);
  return el('span', {className: `badge status-${known ? status : 'unknown'}`}, labels[status] || status || '未知');
}

export function timeText(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value || '—') : date.toLocaleString('zh-CN', {hour12: false});
}

export function pages(start, end) {
  if (start === null || start === undefined) return '页码未标注';
  return end === null || end === undefined || end === start ? `第 ${start} 页` : `第 ${start}–${end} 页`;
}

export function docUrl(version, chunk) {
  const prefix = `${version}:`;
  const mapped = typeof chunk === 'string' && chunk.startsWith(prefix) && /^\d{5,}$/.test(chunk.slice(prefix.length));
  return `#/documents/${encodeURIComponent(version)}${mapped ? `?chunk=${encodeURIComponent(chunk)}` : ''}`;
}

export function sourceLink(source) {
  return el('a', {href: docUrl(source.version_id, source.chunk_id), className: 'source-link'},
    source.source || source.title || source.filename || source.version_id);
}

export function empty(title, detail = '') {
  return el('div', {className: 'empty'}, el('span', {className: 'empty-symbol', 'aria-hidden': 'true'}, '◇'),
    el('h3', {}, title), detail && el('p', {className: 'muted'}, detail));
}

export function heading(title, subtitle, extra) {
  return el('div', {className: 'page-heading'}, el('div', {}, el('h1', {}, title), el('p', {className: 'muted'}, subtitle)), extra);
}

export function table(headers, rows) {
  return el('div', {className: 'table-scroll'}, el('table', {},
    el('thead', {}, el('tr', {}, headers.map(text => el('th', {scope: 'col'}, text)))),
    el('tbody', {}, rows.map(row => el('tr', {}, row.map(cell => el('td', {}, cell)))))));
}

export function docTable(records, {selection = null} = {}) {
  if (!records.length) return empty('暂无符合条件的文档', '可以调整筛选条件，或导入一份新文档。');
  const hasSeries = records.some(row => Object.hasOwn(row, 'series'));
  return table([...(selection ? ['选择'] : []), '文档', ...(hasSeries ? ['系列'] : []), '状态', '页数', '分块', 'OCR', '导入时间', '来源'], records.map(row => [
    ...(selection ? [selection(row)] : []),
    el('div', {className: 'doc-cell'}, el('a', {href: docUrl(row.version_id)}, row.title), el('small', {}, row.filename)),
    ...(hasSeries ? [row.series ? el('a', {href: '#/series/' + encodeURIComponent(row.series.id), className: 'badge series-tag'}, row.series.name) : '—'] : []),
    badge(row.status), row.page_count ?? '—', row.chunk_count ?? '—', row.status === 'purged' ? '—' : row.ocr ? '已开启' : '未开启',
    el('span', {className: 'nowrap muted'}, timeText(row.imported_at)), row.origin === 'seed' ? '种子资料' : '上传',
  ]));
}
