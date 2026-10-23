import {el, api, badge, timeText, docUrl, empty} from './dom.js';
import {can, identityEpoch} from './auth_state.js';

const lifecycleActions = {withdraw: '撤下', restore: '恢复', supersede: '替代', purge: '彻底删除'};
const lifecycleReasons = {manual: '手动撤下', source_missing: '源文件缺失', replaced: '已确认保留其他版本'};
const originText = record => ({seed: '种子资料', scan: '目录扫描', upload: '上传'})[record.origin] || record.origin || '上传';
const changed = () => window.dispatchEvent(new Event('kb:updated'));
const failureText = error => `${({403: '没有权限执行此操作', 404: '文档不存在或不可读', 502: '检索引擎操作失败，请稍后重试'})[error.status] || '操作未完成'}${error.status ? `（${error.status}）` : ''}：${error.message}`;

// 生命周期由接口管理；此处仅确认、提交并重新读取当前页面。
export function lifecycleControls(record, {keep = false, onChanged = changed} = {}) {
  const root = el('div', {className: 'lifecycle-actions'});
  const epoch = identityEpoch(), route = location.hash, controller = new AbortController();
  let disposed = false, dialog = null, busy = false;
  const current = () => !disposed && epoch === identityEpoch() && location.hash === route;
  root.dispose = () => { disposed = true; controller.abort(); dialog?.close(); dialog?.remove(); };
  if (!can('review')) return root;
  const action = keep ? (record.status === 'published' ? 'keep' : null)
    : record.status === 'published' ? 'withdraw' : ['withdrawn', 'superseded'].includes(record.status) ? 'restore' : null;
  if (!action) return root;
  const title = action === 'keep' ? '保留此版本' : lifecycleActions[action];
  const button = el('button', {type: 'button', className: 'button'}, title);
  button.addEventListener('click', () => {
    if (!current() || busy) return;
    dialog?.remove();
    const reason = el('textarea', {rows: 3, name: 'reason', 'aria-label': '撤下原因', placeholder: '可选，最多200字'});
    const error = el('p', {className: 'error-text', role: 'alert'});
    const progress = el('p', {className: 'muted', role: 'status', 'aria-live': 'polite'});
    const submit = el('button', {type: 'submit', className: 'button primary'}, `确认${title}`);
    const cancel = el('button', {type: 'button', className: 'button', onclick: () => dialog.close()}, '取消');
    const form = el('form', {}, el('h2', {}, `确认${title}`), el('p', {className: 'lifecycle-document'}, record.title || record.filename),
      el('p', {className: 'muted'}, action === 'withdraw' ? '撤下后，此版本不再用于检索和问答。记录与分块会保留，可以恢复。'
        : action === 'restore' ? '恢复后，此版本重新用于检索和问答。写入可能需要数秒，不会重新转换或抽取。'
          : '保留此版本后，同一文档中您可读的其他已发布版本将变为“已替代”，不再用于检索和问答。'),
      action === 'withdraw' ? el('label', {}, '撤下原因（可选）', reason) : null,
      error, progress, el('div', {className: 'dialog-actions'}, cancel, submit));
    dialog = el('dialog', {className: 'lifecycle-dialog', 'aria-label': `确认${title}`}, form);
    dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
    dialog.addEventListener('close', () => dialog?.remove());
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (!current() || busy) return;
      if (action === 'withdraw' && Array.from(reason.value.trim()).length > 200) { error.textContent = '原因不能超过200字。'; return; }
      busy = true; button.disabled = submit.disabled = cancel.disabled = reason.disabled = true;
      error.textContent = ''; progress.textContent = '正在处理，请稍候…';
      try {
        await api(`/api/documents/${encodeURIComponent(record.version_id)}/${action}`, {
          method: 'POST', signal: controller.signal, headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(action === 'withdraw' ? {reason: reason.value} : {}),
        });
        if (!current()) return;
        dialog.close();
        onChanged();
      } catch (failure) { if (current()) error.textContent = failureText(failure); }
      finally {
        busy = false;
        if (current()) { button.disabled = submit.disabled = cancel.disabled = reason.disabled = false; progress.textContent = ''; }
      }
    });
    document.body.append(dialog); dialog.showModal();
    (action === 'withdraw' ? reason : cancel).focus();
  });
  root.append(button);
  return root;
}

function versionCard(record) {
  return el('article', {className: 'version-card'},
    el('div', {className: 'panel-heading'}, el('a', {href: docUrl(record.version_id)}, record.title || record.filename), badge(record.status)),
    el('p', {className: 'muted version-meta'}, record.filename, ' · ', record.version_id, ' · ', originText(record), ' · ', timeText(record.imported_at)));
}

export async function versionHistory(version) {
  const {versions} = await api(`/api/documents/${encodeURIComponent(version)}/versions`);
  return el('section', {className: 'panel version-history'}, el('h2', {}, '版本历史'), versions.map(record => {
    const card = versionCard(record);
    card.append(lifecycleHistory(record));
    return card;
  }));
}

export function lifecycleHistory(record) {
  return record.lifecycle?.length ? el('ol', {className: 'lifecycle-history'}, record.lifecycle.map(entry => el('li', {},
      el('strong', {}, lifecycleActions[entry.action] || entry.action), ' · ', timeText(entry.at), ' · ', entry.by,
      entry.reason ? el('p', {}, '原因：', lifecycleReasons[entry.reason] || entry.reason) : null,
      entry.superseded_by ? el('p', {}, '保留版本：', el('a', {href: docUrl(entry.superseded_by)}, entry.superseded_by)) : null)))
      : el('p', {className: 'muted'}, '暂无状态变更记录');
}

export function versionReview(groups, total, onChanged = changed) {
  const root = el('section', {className: 'version-review'}), controls = [], jobs = [];
  const controller = new AbortController(), epoch = identityEpoch();
  let active = false, disposed = false;
  const current = () => active && !disposed && epoch === identityEpoch();
  if (total > groups.length) root.append(el('p', {className: 'warning-banner'}, `共${total}组版本待确认，只显示前100组；处理后刷新可查看后续。`));
  if (!groups.length) root.append(empty('暂无版本待确认', '同一文件夹的同名文档有多个已发布版本时，会显示在这里。'));
  for (const group of groups) {
    const panel = el('section', {className: 'panel version-group'}, el('h2', {}, group[0].filename),
      el('p', {className: 'muted'}, group[0].folder || '根目录', ` · ${group.length}个已发布版本`));
    for (const record of group) {
      const card = versionCard(record), preview = el('p', {className: 'version-preview'}, '正在读取正文摘要…');
      const action = lifecycleControls(record, {keep: true, onChanged}); controls.push(action);
      const retry = el('button', {className: 'button', type: 'button', hidden: true}, '重新读取摘要');
      const load = async () => {
        retry.hidden = true;
        try {
          const data = await api(`/api/documents/${encodeURIComponent(record.version_id)}`, {signal: controller.signal});
          if (current()) preview.textContent = data.chunks.map(chunk => chunk.text).join('\n').slice(0, 240) || '暂无可用正文摘要';
        } catch (error) { if (current()) { preview.textContent = failureText(error); retry.hidden = false; } }
      };
      retry.addEventListener('click', load); jobs.push(load);
      card.append(el('h3', {}, '正文摘要'), preview, retry, action); panel.append(card);
    }
    root.append(panel);
  }
  root.mount = async () => {
    if (disposed || active) return;
    active = true;
    // 限制详情并发，避免一次向服务提交数百个摘要请求。
    let next = 0;
    await Promise.all(Array.from({length: Math.min(4, jobs.length)}, async () => {
      while (current() && next < jobs.length) await jobs[next++]();
    }));
  };
  root.dispose = () => { disposed = true; active = false; controller.abort(); controls.forEach(control => control.dispose()); };
  return root;
}
