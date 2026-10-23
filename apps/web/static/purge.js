import {el, api, badge, heading, timeText} from './dom.js';
import {can, identityEpoch} from './auth_state.js';
import {lifecycleHistory} from './lifecycle.js';

// 墓碑仅展示审计保留字段，不再读取分块、权限、关系或其他版本的内容。
export function tombstoneView(record) {
  const origin = ({seed: '种子资料', scan: '目录扫描', upload: '上传'})[record.origin] || record.origin;
  const fields = [['版本号', record.version_id], ['文件夹', record.folder || '根目录'],
    ['来源', origin], ['导入时间', timeText(record.imported_at)]];
  return el('section', {className: 'tombstone'}, el('a', {href: '#/documents', className: 'back-link'}, '← 返回文档库'),
    heading(record.title, record.filename, badge('purged')),
    el('p', {className: 'warning-banner'}, '此文档已彻底删除，不可恢复。以下为保留的审计记录。'),
    el('dl', {className: 'metadata'}, fields.map(([name, value]) => el('div', {}, el('dt', {}, name), el('dd', {}, value ?? '—')))),
    el('section', {className: 'panel'}, el('h2', {}, '变更记录'), lifecycleHistory(record)));
}

export function purgeReport(report) {
  const rows = [['分块', report.chunks], ['事件', report.events], ['提及', report.mentions],
    ['引用', report.references], ['冲突', report.conflicts], ['已抹去的对话轮次', report.conversation_turns],
    ['已删除的原件与转换对象', report.blobs_deleted]];
  const ids = (title, values) => el('div', {}, el('h3', {}, `${title}（${values.length}）`),
    values.length ? el('ul', {}, values.map(value => el('li', {}, value))) : el('p', {className: 'muted'}, '无'));
  return el('section', {className: 'panel purge-report', role: 'status', 'aria-live': 'polite'},
    el('h2', {}, '删除报告'), el('p', {}, '已彻底删除：', report.version_id),
    el('dl', {className: 'metadata'}, rows.map(([name, value]) => el('div', {}, el('dt', {}, name), el('dd', {}, value)))),
    ids('名称已抹去的实体', report.entities_scrubbed), ids('引文与说明已抹去的审核决定', report.decisions_scrubbed),
    el('p', {}, report.blobs_kept ? '存在其他未删除记录共用原件，原件与转换结果已保留。' : '无其他未删除记录共用原件。'));
}

export function purgeControls(record, onPurged) {
  const root = el('div', {className: 'lifecycle-actions purge-actions'});
  const epoch = identityEpoch(), route = location.hash, controller = new AbortController();
  let disposed = false, busy = false, finished = false, dialog = null;
  const current = () => !disposed && epoch === identityEpoch() && location.hash === route;
  root.dispose = () => { disposed = true; controller.abort(); dialog?.close(); dialog?.remove(); };
  if (!can('purge') || !['withdrawn', 'superseded'].includes(record.status)) return root;
  const button = el('button', {type: 'button', className: 'button danger'}, '彻底删除');
  button.addEventListener('click', () => {
    if (!current() || busy || finished) return;
    dialog?.remove();
    const input = el('input', {type: 'text', name: 'confirm', autocomplete: 'off', spellcheck: false,
      'aria-label': '输入版本号确认彻底删除'});
    const error = el('p', {className: 'error-text', role: 'alert'});
    const progress = el('p', {className: 'muted', role: 'status'});
    const submit = el('button', {type: 'submit', className: 'button danger', disabled: true}, '确认彻底删除');
    const cancel = el('button', {type: 'button', className: 'button', onclick: () => dialog.close()}, '取消');
    input.addEventListener('input', () => { submit.disabled = busy || input.value !== record.version_id; });
    const form = el('form', {}, el('h2', {}, '彻底删除文档'),
      el('p', {className: 'lifecycle-document'}, record.title || record.filename),
      el('p', {className: 'warning-banner'}, '此操作不可恢复。'),
      el('ul', {}, ['清理检索内容、分块、原件及转换结果（共用原件除外）。',
        '清理事件、提及、引用与冲突，抹去无人提及的实体名称。',
        '抹去相关审核决定的引文和说明，以及引用此文档的对话回答。',
        '仅保留文档墓碑和审计记录，源目录中的文件不删除。'].map(text => el('li', {}, text))),
      el('label', {}, '请输入版本号：', el('code', {}, record.version_id), input),
      error, progress, el('div', {className: 'dialog-actions'}, cancel, submit));
    dialog = el('dialog', {className: 'lifecycle-dialog purge-dialog', 'aria-label': '彻底删除文档'}, form);
    dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
    // close 事件异步到达；仅移除触发事件的旧对话框，避免误删刚重新打开的确认框。
    dialog.addEventListener('close', event => event.currentTarget.remove());
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (!current() || busy || finished || input.value !== record.version_id) return;
      busy = true; button.disabled = input.disabled = submit.disabled = cancel.disabled = true;
      error.textContent = ''; progress.textContent = '正在删除，请稍候…';
      try {
        const result = await api(`/api/documents/${encodeURIComponent(record.version_id)}/purge`, {
          method: 'POST', signal: controller.signal, headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({confirm: input.value}),
        });
        if (!current()) return;
        finished = true; dialog.close(); onPurged(result);
      } catch (failure) {
        if (current()) {
          const label = ({403: '没有彻底删除权限', 404: '文档不存在或不可读', 400: '不满足删除条件',
            502: '外部服务操作失败，可重试完成删除', 500: '删除中途失败，可重试完成删除'})[failure.status] || '删除未完成';
          error.textContent = `${label}${failure.status ? `（${failure.status}）` : ''}：${failure.message}`;
        }
      } finally {
        busy = false;
        if (current() && !finished) {
          button.disabled = input.disabled = cancel.disabled = false;
          submit.disabled = input.value !== record.version_id; progress.textContent = '';
        }
      }
    });
    document.body.append(dialog); dialog.showModal(); input.focus();
  });
  root.append(button);
  return root;
}
