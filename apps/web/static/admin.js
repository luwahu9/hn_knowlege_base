import {el, api, post, heading, table, timeText, empty} from './dom.js';
import {can, getIdentity, setIdentity} from './auth_state.js';
import {roles} from './auth.js';

const encoded = encodeURIComponent;
const write = (path, method, body) => api(path, {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
const label = (name, input) => el('label', {}, name, input);
const textInput = (name, value = '') => el('input', {name, value, required: true, maxLength: 64});
const passwordInput = () => el('input', {name: 'password', type: 'password', autocomplete: 'new-password', required: true, minLength: 10, maxLength: 256});
function roleInput(value = 'member') {
  const select = el('select', {name: 'role'}, Object.entries(roles).map(([key, name]) => el('option', {value: key}, name)));
  select.value = value;
  return select;
}
function dialogForm(title, fields, submitAction, submitText = '保存') {
  const dialog = el('dialog', {className: 'account-dialog', 'aria-label': title});
  const error = el('p', {className: 'error-text', role: 'alert'});
  const submit = el('button', {className: 'button primary', type: 'submit'}, submitText);
  const form = el('form', {className: 'auth-form'}, el('h2', {}, title), fields, error,
    el('div', {className: 'dialog-actions'}, el('button', {className: 'button', type: 'button', onclick: () => dialog.close()}, '取消'), submit));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submit.disabled) return;
    submit.disabled = true; error.textContent = '';
    try { await submitAction(); dialog.close(); }
    catch (failure) { error.textContent = failure.message; }
    finally { form.querySelectorAll('input[type="password"]').forEach(input => { input.value = ''; }); submit.disabled = false; }
  });
  dialog.addEventListener('close', () => { form.querySelectorAll('input[type="password"]').forEach(input => { input.value = ''; }); dialog.remove(); });
  dialog.append(form); document.body.append(dialog); dialog.showModal();
  return dialog;
}

async function groupOptions() {
  if (can('manage_users')) return (await api('/api/admin/groups')).groups;
  return getIdentity()?.groups || [];
}
function groupNames(ids, groups) {
  if (!ids?.length) return '禁止所有人';
  const names = new Map(groups.map(row => [row.group_id, row.name]));
  return ids.map(id => names.get(id) || id).join('、');
}
function aclFields(value, groups, includeReason = false) {
  const mode = el('select', {name: 'acl_mode'}, el('option', {value: 'inherit'}, '继承 / 取消例外'),
    el('option', {value: 'deny'}, '禁止所有人'), el('option', {value: 'groups'}, '指定用户组'));
  mode.value = value === null ? 'inherit' : value.length ? 'groups' : 'deny';
  const ids = new Map(groups.map(group => [group.group_id, group.name]));
  for (const id of value || []) if (!ids.has(id)) ids.set(id, id);
  const checkboxes = [...ids].map(([id, name]) => el('input', {type: 'checkbox', value: id, checked: Boolean(value?.includes(id)), name: 'group'}));
  const choices = el('fieldset', {className: 'group-choices'}, el('legend', {}, '授权用户组'),
    [...ids].map(([, name], i) => label(name, checkboxes[i])));
  const update = () => { choices.disabled = mode.value !== 'groups'; };
  mode.addEventListener('change', update); update();
  const reason = el('textarea', {name: 'reason', rows: 3, required: true, maxLength: 500});
  return {fields: [label('授权方式', mode), choices, ...(includeReason ? [label('原因（必填）', reason)] : [])],
    groups: () => mode.value === 'inherit' ? null : mode.value === 'deny' ? [] : checkboxes.filter(item => item.checked).map(item => item.value), reason};
}

export function syncStatusPanel() {
  const element = el('section', {className: 'panel compact'}, el('h3', {}, '检索标签同步'));
  const status = el('p', {className: 'muted', role: 'status'});
  const error = el('p', {className: 'error-text', role: 'alert'});
  element.append(status, error);
  let active = false, disposed = false, timer = null, busy = false, pending = true;
  const controller = new AbortController();
  function schedule() { clearTimeout(timer); if (active && pending) timer = setTimeout(refresh, 3000); }
  async function refresh() {
    if (!active || busy) return;
    busy = true;
    try {
      const data = await api('/api/admin/sync', {signal: controller.signal});
      if (!active) return;
      status.textContent = `待更新 ${data.stale} · 排队 ${data.queued} · 执行中 ${data.running} · 失败 ${data.failed}`;
      error.textContent = '';
      pending = data.queued > 0 || data.running > 0;
    } catch (failure) { if (active) error.textContent = failure.message; }
    finally { busy = false; schedule(); }
  }
  return {element, refresh, async mount() { if (!disposed) { active = true; await refresh(); } },
    dispose() { disposed = true; active = false; clearTimeout(timer); controller.abort(); }};
}

export async function documentPermissions(version, onChanged = () => window.dispatchEvent(new Event('kb:updated'))) {
  const [data, groups] = await Promise.all([api(`/api/admin/documents/${encoded(version)}/acl`), groupOptions()]);
  const error = el('p', {className: 'error-text', role: 'alert'});
  const root = el('section', {className: 'panel document-permissions'}, el('h2', {}, '权限'),
    el('p', {}, '所在文件夹：', data.folder || '根目录'), el('p', {}, '有效授权：', groupNames(data.effective, groups)),
    el('p', {}, '单文档例外：', data.override === null ? '未设置（继承文件夹）' : groupNames(data.override, groups)), error);
  root.append(el('button', {className: 'button', type: 'button', onclick: () => {
    const fields = aclFields(data.override, groups, true);
    dialogForm('设置单文档例外 · ' + data.title, [el('p', {className: 'muted'}, version, ' · ', data.folder || '根目录'), ...fields.fields], async () => {
      if (!fields.reason.value.trim()) throw new Error('必须填写原因');
      await write(`/api/admin/documents/${encoded(version)}/acl`, 'PUT', {groups: fields.groups(), reason: fields.reason.value});
      await onChanged();
    });
  }}, '设置或取消例外'));
  return root;
}

function historyTable(rows, groups) {
  return rows.length ? table(['时间', '操作者', '类型', '对象', '授权', '原因'], rows.map(row => [
    timeText(row.at), row.actor || '—', row.kind === 'folder' ? '文件夹' : '单文档', row.target || '根目录',
    row.groups === null ? '继承 / 取消例外' : groupNames(row.groups, groups), row.reason || '—'])) : empty('暂无授权审计');
}

export async function adminView() {
  const page = el('section', {className: 'admin-page'}, heading('系统管理', '管理账号、用户组与资料访问权限。'));
  const body = el('div', {className: 'admin-content'});
  let disposed = false, revision = 0, tabDispose = null;
  const tabs = [];
  const available = [ ...(can('manage_users') ? [['users', '账号'], ['groups', '用户组']] : []),
    ...(can('manage_acl') ? [['folders', '文件夹授权']] : []), ...(can('manage_users') ? [['audit', '审计']] : []) ];
  async function paint(kind) {
    const current = ++revision;
    tabDispose?.(); tabDispose = null;
    tabs.forEach(([button, key]) => { button.classList.toggle('active', key === kind); button.setAttribute('aria-selected', String(key === kind)); });
    body.replaceChildren(el('div', {className: 'loading'}, '正在加载…'));
    try {
      const content = kind === 'users' ? await usersPage() : kind === 'groups' ? await groupsPage()
        : kind === 'folders' ? await foldersPage() : await auditPage();
      if (disposed || current !== revision) { content.dispose?.(); return; }
      body.replaceChildren(content); tabDispose = content.dispose || null;
      await content.mount?.();
    } catch (failure) { if (!disposed && current === revision) body.replaceChildren(empty('暂时无法加载', failure.message)); }
  }
  const tabbar = el('div', {className: 'tabs', role: 'tablist', 'aria-label': '系统管理内容'});
  for (const [key, name] of available) {
    const button = el('button', {className: 'tab', type: 'button', role: 'tab', onclick: () => paint(key)}, name);
    tabs.push([button, key]); tabbar.append(button);
  }
  page.append(tabbar, body);
  page.dispose = () => { disposed = true; revision++; tabDispose?.(); };
  async function userChanged(userId) {
    if (userId === getIdentity()?.user.user_id) {
      setIdentity(await api('/api/auth/me'));
      window.dispatchEvent(new Event('hashchange'));
    } else await paint('users');
  }

  async function usersPage() {
    const [{users}, {groups}] = await Promise.all([api('/api/admin/users'), api('/api/admin/groups')]);
    const section = el('section', {className: 'panel'});
    const error = el('p', {className: 'error-text', role: 'alert'});
    section.append(el('div', {className: 'panel-heading'}, el('h2', {}, '账号'), el('button', {className: 'button primary', type: 'button', onclick: () => {
      const name = textInput('username'), display = textInput('display_name'), role = roleInput(), password = passwordInput();
      dialogForm('新建账号', [label('登录名', name), label('显示名', display), label('角色', role), label('初始密码', password)], async () => {
        await post('/api/admin/users', {username: name.value, display_name: display.value, role: role.value, password: password.value});
        await paint('users');
      });
    }}, '新建账号')), error);
    section.append(table(['登录名', '显示名', '角色', '状态', '所在组', '操作'], users.map(user => [
      user.username, user.display_name, roles[user.role] || user.role, user.active ? '启用' : '停用',
      [user.active ? '全体员工' : '', ...user.groups.map(id => groups.find(group => group.group_id === id)?.name || id)].filter(Boolean).join('、') || '—',
      el('div', {className: 'admin-row-actions'}, el('button', {className: 'button', type: 'button', onclick: () => {
        const name = textInput('display_name', user.display_name), role = roleInput(user.role);
        dialogForm('修改账号 · ' + user.username, [label('显示名', name), label('角色', role)], async () => {
          await write('/api/admin/users/' + encoded(user.user_id), 'PATCH', {display_name: name.value, role: role.value});
          await userChanged(user.user_id);
        });
      }}, '修改'), el('button', {className: 'button', type: 'button', onclick: async event => {
        event.target.disabled = true; error.textContent = '';
        try { await write('/api/admin/users/' + encoded(user.user_id), 'PATCH', {active: !user.active}); await userChanged(user.user_id); }
        catch (failure) { error.textContent = failure.message; }
        finally { event.target.disabled = false; }
      }}, user.active ? '停用' : '启用'), el('button', {className: 'button', type: 'button', onclick: () => {
        const password = passwordInput();
        dialogForm('重置密码 · ' + user.username, [label('新密码', password)], async () => {
          await post('/api/admin/users/' + encoded(user.user_id) + '/password', {password: password.value});
          await userChanged(user.user_id);
        });
      }}, '重置密码'))])));
    return section;
  }

  async function groupsPage() {
    const [{groups}, {users}] = await Promise.all([api('/api/admin/groups'), api('/api/admin/users')]);
    const section = el('section', {className: 'panel'}), error = el('p', {className: 'error-text', role: 'alert'});
    section.append(el('div', {className: 'panel-heading'}, el('h2', {}, '用户组'), el('button', {className: 'button primary', type: 'button', onclick: () => {
      const name = textInput('name');
      dialogForm('新建用户组', [label('组名', name)], async () => { await post('/api/admin/groups', {name: name.value}); await paint('groups'); });
    }}, '新建用户组')), error);
    section.append(table(['用户组', '成员', '操作'], groups.map(group => [group.name,
      group.builtin ? '所有启用的账号' : (group.members || []).map(id => users.find(user => user.user_id === id)?.display_name || id).join('、') || '暂无成员',
      group.builtin ? el('span', {className: 'muted'}, '内置组 · 只读') : el('div', {className: 'admin-row-actions'},
        el('button', {className: 'button', type: 'button', onclick: () => {
          const name = textInput('name', group.name);
          dialogForm('修改组名', [label('组名', name)], async () => { await write('/api/admin/groups/' + encoded(group.group_id), 'PATCH', {name: name.value}); await paint('groups'); });
        }}, '改名'), el('button', {className: 'button', type: 'button', onclick: () => {
          const choices = el('select', {name: 'user_ids', multiple: true, size: Math.min(10, Math.max(3, users.length))}, users.map(user =>
            el('option', {value: user.user_id, selected: group.members.includes(user.user_id)}, user.display_name, '（', user.username, '）')));
          dialogForm('编辑成员 · ' + group.name, [label('按住 Ctrl 或 Command 多选账号', choices)], async () => {
            await write('/api/admin/groups/' + encoded(group.group_id) + '/members', 'PUT', {user_ids: Array.from(choices.selectedOptions, item => item.value)}); await paint('groups');
          });
        }}, '编辑成员'), el('button', {className: 'button', type: 'button', onclick: () => {
          dialogForm('删除用户组', [el('p', {}, '确认删除“', group.name, '”？')], async () => {
            await api('/api/admin/groups/' + encoded(group.group_id), {method: 'DELETE'}); await paint('groups');
          }, '确认删除');
        }}, '删除'))])));
    return section;
  }

  async function foldersPage() {
    const [{folders}, groups, {entries}] = await Promise.all([api('/api/admin/folders'), groupOptions(), api('/api/admin/acl/history')]);
    const section = el('section', {className: 'admin-folders'}), notice = el('p', {className: 'muted', role: 'status'});
    const sync = syncStatusPanel();
    section.append(el('p', {className: 'warning-banner'}, '管理员也需要在授权组内才能阅读文档'), notice, sync.element);
    const children = new Map();
    for (const folder of folders) { const key = folder.parent; if (!children.has(key)) children.set(key, []); children.get(key).push(folder); }
    function tree(parent) {
      return el('ul', {className: 'acl-tree'}, (children.get(parent) || []).map(folder => el('li', {},
        el('div', {className: 'panel folder-acl-row'}, el('h3', {}, folder.name || '根目录'), el('small', {className: 'muted'}, folder.path || '根目录'),
          el('p', {}, '显式授权：', folder.explicit === null ? '继承' : groupNames(folder.explicit, groups)),
          el('p', {}, '有效授权：', groupNames(folder.effective, groups)), el('p', {className: 'muted'}, `文档 ${folder.documents} · 邮件 ${folder.mails || 0} · 例外 ${folder.overrides}`),
          el('div', {className: 'admin-row-actions'}, el('button', {className: 'button', type: 'button', onclick: () => {
            const fields = aclFields(folder.explicit, groups);
            dialogForm('文件夹授权 · ' + (folder.path || '根目录'), fields.fields, async () => {
              const result = await write('/api/admin/folders/acl', 'PUT', {path: folder.path, groups: fields.groups()});
              await paint('folders');
              const message = body.querySelector?.('[data-acl-result]');
              if (message) message.textContent = `已更新授权，受影响文档 ${result.affected} 份`;
            });
          }}, '设置授权'), el('button', {className: 'button', type: 'button', onclick: () => {
            const version = textInput('version_id'); version.maxLength = 200;
            dialogForm('按版本号设置例外', [el('p', {className: 'muted'}, '可管理不可读文档的权限，不会显示正文。'), label('版本号', version)], async () => {
              const content = await documentPermissions(version.value.trim(), async () => { await paint('folders'); popup.close(); });
              const popup = el('dialog', {className: 'account-dialog', 'aria-label': '文档权限'}, content,
                el('button', {className: 'button', onclick: () => popup.close()}, '关闭'));
              popup.addEventListener('close', () => popup.remove()); document.body.append(popup); popup.showModal();
            }, '查找文档');
          }}, '按版本号设置例外'))), tree(folder.path))));
    }
    notice.setAttribute('data-acl-result', '1');
    section.append(tree(null), el('section', {className: 'panel'}, el('h2', {}, '授权审计'), historyTable(entries, groups)));
    section.mount = sync.mount; section.dispose = sync.dispose;
    return section;
  }

  async function auditPage() {
    const section = el('section', {className: 'panel'}), target = el('input', {name: 'target', placeholder: '对象 ID（可空）', 'aria-label': '按审计对象过滤'});
    const rows = el('div'), error = el('p', {className: 'error-text', role: 'alert'});
    let before = null, trail = [], data = [], request = 0, closed = false;
    const previous = el('button', {className: 'button', type: 'button', onclick: () => { before = trail.pop() ?? null; return refresh(); }}, '上一页');
    const next = el('button', {className: 'button', type: 'button', onclick: () => { trail.push(before); before = data.at(-1).seq; return refresh(); }}, '下一页');
    async function refresh() {
      const current = ++request; previous.disabled = next.disabled = true;
      try {
        const query = new URLSearchParams({limit: '50'});
        if (target.value.trim()) query.set('target', target.value.trim());
        if (before !== null) query.set('before', before);
        const result = await api('/api/admin/audit?' + query);
        if (closed || current !== request) return;
        data = result.entries; error.textContent = '';
        rows.replaceChildren(data.length ? table(['时间', '操作者', '操作', '对象', '详情'], data.map(row => [
          timeText(row.at), row.actor || '—', row.action, row.target || '—', el('pre', {className: 'audit-detail'}, JSON.stringify(row.detail, null, 2))])) : empty('暂无账号审计'));
      } catch (failure) { if (!closed && current === request) error.textContent = failure.message; }
      finally { if (!closed && current === request) { previous.disabled = !trail.length; next.disabled = data.length < 50; } }
    }
    section.append(el('h2', {}, '账号审计'), el('form', {className: 'filter-bar', onsubmit: event => { event.preventDefault(); before = null; trail = []; return refresh(); }},
      target, el('button', {className: 'button', type: 'submit'}, '筛选')), error, rows, el('div', {className: 'dialog-actions'}, previous, next));
    section.dispose = () => { closed = true; request++; };
    await refresh(); return section;
  }
  if (available.length) await paint(available[0][0]);
  return page;
}
