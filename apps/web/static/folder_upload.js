import {el, api, docUrl} from './dom.js';
import {requestOptions, unauthorized, identityEpoch, requireEpoch, getIdentity} from './auth_state.js';

const stateLabels = {pending: '等待处理', hashing: '正在计算哈希', checking: '正在查重', exists: '已存在',
  importing: '导入中', skipped: '已跳过', uploading: '上传中', submitted: '已提交', failed: '失败'};

// 仅网络错误重试；PUT 的响应丢失由服务端的偏移冲突恢复。
async function request(url, options = {}, epoch = identityEpoch()) {
  for (let attempt = 0; ; attempt++) {
    requireEpoch(epoch);
    let response, data;
    try { response = await fetch(url, requestOptions(options)); data = await response.json(); }
    catch (error) {
      requireEpoch(epoch);
      if (response?.status === 401) unauthorized();
      if (!(error instanceof TypeError)) throw error;
      if (attempt === 3) throw new Error('网络连接失败，请重试失败项。');
      await new Promise(resolve => setTimeout(resolve, 1000 * 2 ** attempt));
      continue;
    }
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
}

function jsonRequest(url, body, epoch) {
  return request(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}, epoch);
}

export class FolderImport {
  constructor(onChange) { this.items = []; this.busy = false; this.onChange = onChange; }

  select(files) {
    if (this.busy) return;
    this.items = Array.from(files, file => ({file, path: file.webkitRelativePath || file.importPath || file.name, state: 'pending'}));
    this.onChange();
  }

  async start(retry = false) {
    if (this.busy) return;
    const pending = this.items.filter(item => item.state === (retry ? 'failed' : 'pending'));
    if (!pending.length) return;
    this.busy = true;
    const epoch = identityEpoch();
    this.onChange();
    try {
      const config = await request('/api/uploads/config', {}, epoch);
      const extensions = new Set(config.extensions);
      const ready = [];
      let batch = [];
      const check = async () => {
        const group = batch;
        batch = [];
        try {
          const {results} = await jsonRequest('/api/uploads/check', {files: group.map(item => this._description(item))}, epoch);
          if (results.length !== group.length) throw new Error('查重结果数量不符');
          results.forEach((result, index) => {
            const item = group[index];
            if (result.state === 'new' || result.state === 'partial') ready.push(item);
            else if (result.state === 'rejected') Object.assign(item, {state: 'skipped', error: result.error});
            else Object.assign(item, result);
          });
        } catch (error) { group.forEach(item => Object.assign(item, {state: 'failed', error: error.message})); }
        this.onChange();
      };
      for (const item of pending) {
        requireEpoch(epoch);
        item.error = '';
        const extension = item.file.name.includes('.') ? item.file.name.split('.').pop().toLowerCase() : '';
        if (!extensions.has(extension) || item.file.size > config.max_bytes) {
          item.state = 'skipped';
          item.error = !extensions.has(extension) ? '暂不支持该格式' : `文件超过 ${config.max_bytes} 字节上限`;
          this.onChange();
          continue;
        }
        try {
          item.state = 'hashing'; this.onChange();
          const digest = await crypto.subtle.digest('SHA-256', await item.file.arrayBuffer());
          item.sha256 = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
          item.state = 'checking'; batch.push(item);
        } catch (error) { Object.assign(item, {state: 'failed', error: error.message}); }
        this.onChange();
        if (batch.length === config.check_batch) await check();
      }
      if (batch.length) await check();
      let cursor = 0;
      const uploads = new Map();
      const worker = async () => {
        while (cursor < ready.length) {
          const item = ready[cursor++];
          // 相同内容共用一个暂存 ID，不能同时写入或完成同一上传。
          const previous = uploads.get(item.sha256) || Promise.resolve();
          const current = previous.catch(() => {}).then(() => this._upload(item, config.chunk_bytes, epoch));
          uploads.set(item.sha256, current);
          try { await current; }
          catch (error) { Object.assign(item, {state: 'failed', error: error.message}); }
          this.onChange();
        }
      };
      await Promise.all(Array.from({length: Math.min(3, ready.length)}, worker));
    } catch (error) {
      pending.filter(item => !['skipped', 'exists', 'importing', 'submitted', 'failed'].includes(item.state))
        .forEach(item => Object.assign(item, {state: 'failed', error: error.message}));
    } finally { this.busy = false; this.onChange(); }
  }

  _description(item) { return {path: item.path, size: item.file.size, sha256: item.sha256}; }

  async _upload(item, chunkBytes, epoch = identityEpoch()) {
    try {
      const begin = await jsonRequest('/api/uploads', this._description(item), epoch);
      item.state = 'uploading'; item.received = begin.received; this.onChange();
      const url = `/api/uploads/${encodeURIComponent(begin.upload_id)}`;
      while (item.received < item.file.size) {
        const offset = item.received;
        let next;
        try {
          const data = await request(`${url}?offset=${offset}`, {method: 'PUT',
            body: item.file.slice(offset, Math.min(offset + chunkBytes, item.file.size))}, epoch);
          next = data.received;
        } catch (error) {
          if (error.status !== 409) throw error;
          next = error.data.received;
        }
        if (!Number.isInteger(next) || next < 0 || next > item.file.size || next === offset)
          throw new Error('服务返回了无效的续传位置');
        item.received = next; this.onChange();
      }
      let result;
      try { result = await request(`${url}/complete`, {method: 'POST'}, epoch); }
      catch (error) {
        if (error.status !== undefined && error.status !== 404) throw error;
        // 完成成功后暂存已删除；响应丢失时按哈希确认最终状态。
        const {results} = await jsonRequest('/api/uploads/check', {files: [this._description(item)]}, epoch);
        const found = results[0];
        if (!found || !['exists', 'importing'].includes(found.state)) throw error;
        Object.assign(item, found);
        return;
      }
      Object.assign(item, {state: 'submitted', job_id: result.job_id});
    } catch (error) {
      if (error.status !== 409 || !error.data.version_id) throw error;
      Object.assign(item, {state: error.data.job_id ? 'importing' : 'exists', version_id: error.data.version_id,
        job_id: error.data.job_id});
    }
  }
}

async function entryFiles(entry, parent = '') {
  const path = `${parent}${entry.name}`;
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    Object.defineProperty(file, 'importPath', {value: path});
    return [file];
  }
  if (!entry.isDirectory) return [];
  const reader = entry.createReader();
  const files = [];
  while (true) {
    const entries = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
    if (!entries.length) return files;
    for (const child of entries) files.push(...await entryFiles(child, `${path}/`));
  }
}

export function initFolderUpload() {
  const dialog = el('dialog', {id: 'folder-dialog', 'aria-labelledby': 'folder-title'});
  const picker = el('input', {type: 'file', id: 'folder-files', className: 'sr-only', multiple: true, webkitdirectory: true});
  const list = el('div', {className: 'folder-upload-list', 'aria-live': 'polite'});
  const summary = el('p', {className: 'muted', id: 'folder-summary'});
  const taskSummary = el('p', {className: 'muted'});
  const start = el('button', {className: 'button primary', type: 'button'}, '开始导入');
  const retry = el('button', {className: 'button', type: 'button'}, '重试失败项');
  const close = el('button', {className: 'icon-button', 'aria-label': '关闭文件夹导入', onclick: () => dialog.close()}, '×');
  const drop = el('label', {className: 'file-picker folder-drop', htmlFor: 'folder-files'},
    el('span', {className: 'upload-symbol'}, '▤'), el('strong', {}, '选择文件夹，或将文件夹拖到这里'),
    el('span', {className: 'muted'}, '保留目录结构 · 自动查重 · 支持断点续传'));
  const importer = new FolderImport(paint);
  let timer = null, polling = false;
  window.addEventListener('kb:identity-changed', () => {
    clearTimeout(timer); importer.items = []; picker.value = ''; taskSummary.textContent = ''; paint();
  });
  function paint() {
    const counts = {};
    for (const item of importer.items) counts[item.state] = (counts[item.state] || 0) + 1;
    summary.textContent = !importer.items.length ? '尚未选择文件夹' : `共 ${importer.items.length} 份 · ` +
      Object.entries(counts).map(([state, count]) => `${stateLabels[state] || state} ${count}`).join(' · ');
    list.replaceChildren(...importer.items.slice(0, 500).map(item => el('div', {className: 'upload-item'},
      el('div', {className: 'upload-file-info'}, el('strong', {}, item.path),
        el('small', {}, `${(item.file.size / 1000).toFixed(1)} KB`)),
      el('div', {className: 'upload-result'}, el('span', {className: item.state === 'failed' ? 'error-text' : 'muted'},
        `${stateLabels[item.state] || item.state}${item.state === 'uploading' ? ` ${Math.floor(item.received / item.file.size * 100)}%` : ''}`),
        item.version_id ? el('a', {href: docUrl(item.version_id), onclick: () => dialog.close()}, '查看文档') : null,
        item.error ? el('small', {className: item.state === 'failed' ? 'error-text' : 'muted'}, item.error) : null))));
    if (importer.items.length > 500) list.append(el('p', {className: 'muted'}, `仅显示前 500 行，其余 ${importer.items.length - 500} 份已计入汇总。`));
    start.disabled = importer.busy || !counts.pending;
    retry.disabled = importer.busy || !counts.failed;
    picker.disabled = importer.busy;
  }
  async function progress() {
    if (polling || !getIdentity()) return;
    const epoch = identityEpoch();
    polling = true;
    clearTimeout(timer);
    try {
      const {ingest} = await api('/api/tasks');
      taskSummary.textContent = `文档导入：排队 ${ingest.queued} · 执行中 ${ingest.running} · 已完成 ${ingest.done} · 失败 ${ingest.failed}`;
      if (ingest.queued || ingest.running) timer = setTimeout(progress, 3000);
      else window.dispatchEvent(new Event('kb:updated'));
    } catch (error) { if (epoch === identityEpoch()) { taskSummary.textContent = error.message; timer = setTimeout(progress, 3000); } }
    finally { polling = false; }
  }
  async function run(retryFailed) {
    await importer.start(retryFailed);
    window.dispatchEvent(new Event('kb:updated'));
    if (importer.items.some(item => ['submitted', 'importing'].includes(item.state))) progress();
  }
  picker.addEventListener('change', () => importer.select(picker.files));
  start.addEventListener('click', () => run(false));
  retry.addEventListener('click', () => run(true));
  drop.addEventListener('dragover', event => { event.preventDefault(); });
  drop.addEventListener('drop', async event => {
    event.preventDefault();
    if (importer.busy) return;
    const epoch = identityEpoch();
    try {
      const entries = Array.from(event.dataTransfer.items || [], item => item.webkitGetAsEntry?.()).filter(Boolean);
      const files = [];
      for (const entry of entries) files.push(...await entryFiles(entry));
      requireEpoch(epoch);
      importer.select(files.length ? files : event.dataTransfer.files);
    } catch (error) { if (epoch === identityEpoch()) summary.textContent = `无法读取文件夹：${error.message}`; }
  });
  window.addEventListener('beforeunload', event => { if (importer.busy) { event.preventDefault(); event.returnValue = ''; } });
  dialog.append(el('div', {className: 'dialog-heading'}, el('div', {}, el('h2', {id: 'folder-title'}, '导入文件夹'),
    el('p', {className: 'muted'}, '重新选择同一文件夹即可查重或继续未完成的上传。')), close), drop, picker, list, summary,
    taskSummary, el('div', {className: 'dialog-actions'}, retry, start));
  document.body.append(dialog);
  document.querySelector('#open-folder-upload').addEventListener('click', () => dialog.showModal());
  paint();
}
