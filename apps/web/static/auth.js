import {el, api, post} from './dom.js';
import {getIdentity, setIdentity, unauthorized} from './auth_state.js';

export const roles = {admin: '管理员', reviewer: '审核员', member: '成员'};
export function loginView(onSuccess, message = '') {
  const username = el('input', {name: 'username', autocomplete: 'username', required: true, maxLength: 64, 'aria-label': '登录名'});
  const password = el('input', {name: 'password', type: 'password', autocomplete: 'current-password', required: true, maxLength: 256, 'aria-label': '密码'});
  const error = el('p', {className: 'error-text', role: 'alert'}, message);
  const submit = el('button', {type: 'submit', className: 'button primary'}, '登录');
  const form = el('form', {className: 'auth-form'}, el('label', {}, '登录名', username), el('label', {}, '密码', password), error, submit);
  const page = el('section', {className: 'login-page'}, el('div', {className: 'panel login-card'},
    el('span', {className: 'brand-mark'}, '海'), el('h1', {}, '登录海纳知识库'),
    el('p', {className: 'muted'}, '使用你的账号继续访问知识库。'), form));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submit.disabled) return;
    submit.disabled = true; error.textContent = '';
    const value = password.value;
    password.value = '';
    try {
      await post('/api/auth/login', {username: username.value, password: value});
      setIdentity(await api('/api/auth/me'));
      await onSuccess();
    } catch (failure) {
      if (failure.status === 429 && failure.data?.until) {
        const date = new Date(failure.data.until);
        const hhmm = Number.isNaN(date.getTime()) ? '稍后' : date.toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', hour12: false});
        error.textContent = `尝试次数过多，请于 ${hhmm} 后再试`;
      } else error.textContent = failure.message;
    } finally { submit.disabled = false; }
  });
  return page;
}

export function accountMenu() {
  const current = getIdentity();
  if (!current?.auth_enabled) return el('span', {hidden: true});
  const error = el('p', {className: 'error-text', role: 'alert'});
  const menu = el('details', {className: 'account-menu'}, el('summary', {}, current.user.display_name, ' · ', roles[current.user.role] || current.user.role));
  menu.append(el('div', {className: 'panel account-options'},
    el('button', {className: 'button', type: 'button', onclick: () => { menu.open = false; passwordDialog(); }}, '修改密码'),
    el('button', {className: 'button', type: 'button', onclick: async event => {
      event.target.disabled = true;
      try { await post('/api/auth/logout', {}); unauthorized(); }
      catch (failure) { error.textContent = failure.message; }
      finally { event.target.disabled = false; }
    }}, '退出登录'), error));
  return menu;
}

function passwordDialog() {
  const old = el('input', {name: 'old', type: 'password', autocomplete: 'current-password', required: true, maxLength: 256});
  const value = el('input', {name: 'new', type: 'password', autocomplete: 'new-password', required: true, minLength: 10, maxLength: 256});
  const confirm = el('input', {name: 'confirm', type: 'password', autocomplete: 'new-password', required: true, minLength: 10, maxLength: 256});
  const error = el('p', {className: 'error-text', role: 'alert'});
  const submit = el('button', {className: 'button primary', type: 'submit'}, '保存新密码');
  const dialog = el('dialog', {className: 'account-dialog', 'aria-labelledby': 'password-title'});
  const form = el('form', {className: 'auth-form'}, el('h2', {id: 'password-title'}, '修改密码'),
    el('label', {}, '原密码', old), el('label', {}, '新密码', value), el('label', {}, '确认新密码', confirm), error,
    el('div', {className: 'dialog-actions'}, el('button', {className: 'button', type: 'button', onclick: () => dialog.close()}, '取消'), submit));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submit.disabled) return;
    if (value.value !== confirm.value) { error.textContent = '两次输入的新密码不一致'; return; }
    submit.disabled = true; error.textContent = '';
    const body = {old: old.value, new: value.value};
    old.value = value.value = confirm.value = '';
    try { await post('/api/auth/password', body); dialog.close(); }
    catch (failure) { error.textContent = failure.message; }
    finally { submit.disabled = false; }
  });
  dialog.append(form);
  dialog.addEventListener('close', () => { old.value = value.value = confirm.value = ''; dialog.remove(); });
  document.body.append(dialog); dialog.showModal();
}
