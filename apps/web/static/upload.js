import {el, api, badge, docUrl} from './dom.js';
import {identityEpoch, requireEpoch} from './auth_state.js';

export const jobStages = {queued: '排队中', converting: '转换、质检与切块中', publishing: '正在校验索引', done: '处理完成', failed: '处理失败'};

export async function pollJob(id, onUpdate, {signal} = {}) {
  const epoch = identityEpoch();
  while (!signal?.aborted) {
    requireEpoch(epoch);
    let job;
    try { job = await api(`/api/jobs/${encodeURIComponent(id)}`, {signal}); }
    catch (error) { if (signal?.aborted) return null; throw error; }
    if (signal?.aborted) return null;
    onUpdate(job);
    if (job.status === 'done' || job.status === 'failed') return job;
    await new Promise(resolve => {
      function finish() { clearTimeout(timer); signal?.removeEventListener('abort', finish); resolve(); }
      const timer = setTimeout(finish, 2000);
      signal?.addEventListener('abort', finish, {once: true});
      if (signal?.aborted) finish();
    });
  }
  return null;
}

export function initUpload() {
  const dialog = document.querySelector('#upload-dialog');
  const picker = document.querySelector('#upload-files');
  const list = document.querySelector('#upload-list');
  const start = document.querySelector('#start-upload');
  const summary = document.querySelector('#upload-summary');
  let items = [];
  let busy = false;
  window.addEventListener('kb:identity-changed', () => { items = []; picker.value = ''; paint(); });
  document.querySelector('#open-upload').addEventListener('click', () => dialog.showModal());
  document.querySelector('#close-upload').addEventListener('click', () => dialog.close());
  function paint() {
    list.replaceChildren(...items.map(item => el('div', {className: 'upload-item'},
      el('div', {className: 'upload-file-info'}, el('strong', {}, item.file.name), el('small', {}, `${(item.file.size / 1000).toFixed(1)} KB`)),
      item.document ? el('div', {className: 'upload-result'}, badge(item.document.status),
        el('a', {href: docUrl(item.document.version_id), onclick: () => dialog.close()}, '查看文档'))
        : el('span', {className: item.error ? 'error-text' : 'muted'}, item.error || jobStages[item.stage] || '等待提交'),
      item.error && item.document ? el('p', {className: 'error-text'}, item.error) : null)));
    const finished = items.filter(item => item.finished).length;
    summary.textContent = !items.length ? '尚未选择文件' : busy ? `正在处理 · ${finished} / ${items.length} 份已结束` :
      finished ? `处理结束 · ${finished} / ${items.length} 份` : `已选择 ${items.length} 份文件`;
    start.disabled = busy || !items.length || finished === items.length;
    picker.disabled = busy;
    start.textContent = busy ? '正在导入…' : '开始导入';
  }
  picker.addEventListener('change', () => { items = Array.from(picker.files, file => ({file})); paint(); });
  async function poll(item, id) {
    await pollJob(id, job => {
      item.stage = job.stage;
      item.document = job.document;
      item.error = job.error;
      paint();
    });
  }
  start.addEventListener('click', async () => {
    if (busy || !items.length) return;
    busy = true;
    const epoch = identityEpoch();
    paint();
    const pending = [];
    for (const item of items) {
      if (epoch !== identityEpoch()) break;
      try {
        const form = new FormData();
        form.append('file', item.file);
        const job = await api('/api/documents', {method: 'POST', body: form});
        requireEpoch(epoch);
        item.stage = 'queued';
        window.dispatchEvent(new Event('kb:updated'));
        pending.push(poll(item, job.job_id).catch(error => { item.error = error.message; }).finally(() => {
          item.finished = true;
          paint();
          window.dispatchEvent(new Event('kb:updated'));
        }));
      } catch (error) { item.error = error.message; item.finished = true; }
      paint();
    }
    await Promise.all(pending);
    busy = false;
    paint();
    window.dispatchEvent(new Event('kb:updated'));
  });
}
