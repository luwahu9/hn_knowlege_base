import './theme.js';
import {seriesListView, seriesDetailView} from './series.js';
import {el, api, empty} from './dom.js';
import {askView, overviewView, documentsView, detailView, reviewView, searchView, clearPrivateState} from './views.js';
import {timelineView, graphView} from './knowledge.js';
import {initUpload} from './upload.js';
import {initFolderUpload} from './folder_upload.js';
import {getIdentity, setIdentity, can, allowedPage, identityEpoch} from './auth_state.js';
import {loginView, accountMenu} from './auth.js';
import {adminView} from './admin.js';
import {mailView, mailDetailView} from './mail.js';

const names = {series: '文档系列', ask: '小安检索', overview: '总览', documents: '文档库', review: '审核中心', graph: '知识图谱', timeline: '时间轴', search: '全局搜索', admin: '系统管理', mail: '邮件'};
const view = document.querySelector('#view');
const account = el('div', {id: 'account'});
document.querySelector('.topbar').append(account);
document.querySelectorAll('nav a').forEach(link => {
  const text = [...link.childNodes].filter(node => node.nodeType === Node.TEXT_NODE).map(node => node.textContent).join('').trim();
  link.title = text;
  [...link.childNodes].filter(node => node.nodeType === Node.TEXT_NODE).forEach(node => node.remove());
  link.append(el('span', {className: 'nav-name'}, text));
});
let revision = 0;
let disposeView = null;
let login = null;
let booting = true;

function shell() {
  const current = getIdentity();
  document.body.classList.remove('auth-pending');
  document.body.classList.toggle('auth-locked', !current);
  document.querySelectorAll('nav a').forEach(link => { link.hidden = !allowedPage(link.dataset.page); });
  document.querySelector('#open-upload').hidden = !can('upload');
  document.querySelector('#open-folder-upload').hidden = !can('upload');
  account.replaceChildren(accountMenu());
}
function showLogin(message = '') {
  revision++; disposeView?.(); disposeView = null;
  shell();
  document.title = '登录 · 海纳知识库';
  if (login) return;
  login = loginView(async () => {
    login = null; shell(); health();
    if (!location.hash) location.hash = '#/ask'; else await route();
  }, message === '请先登录' ? '' : message);
  view.replaceChildren(login);
  health();
}
async function bootstrap() {
  booting = true;
  try {
    setIdentity(await api('/api/auth/me'));
    booting = false;
    login = null; shell(); health();
    if (!location.hash) location.hash = '#/ask'; else await route();
  } catch (error) {
    document.body.classList.remove('auth-pending');
    if (error.status === 401) showLogin();
    else {
      document.body.classList.add('auth-locked');
      view.replaceChildren(empty('暂时无法连接知识库', error.message), el('button', {className: 'button', onclick: bootstrap}, '重试'));
    }
  } finally { booting = false; }
}
window.addEventListener('kb:identity-changed', () => {
  revision++; disposeView?.(); disposeView = null; clearPrivateState();
  document.querySelector('#global-query').value = '';
  document.querySelectorAll('dialog').forEach(dialog => { if (dialog.open) dialog.close(); });
  document.querySelectorAll('input[type="password"]').forEach(input => { input.value = ''; });
  if (!getIdentity()) { login = null; view.replaceChildren(); }
  if (!booting) shell();
});
window.addEventListener('kb:unauthorized', event => { if (!booting) showLogin(event.detail); });

async function health() {
  const epoch = identityEpoch();
  const label = document.querySelector('#dataset-name');
  try {
    const data = await api('/api/health');
    if (epoch !== identityEpoch()) return;
    document.querySelector('#model-location-notice').hidden = data.model_location !== 'online';
    label.textContent = data.auth ? '本机知识库 · 账号模式' : `${data.dataset_name} · ${data.documents} 份资料`;
    label.className = '';
  } catch { if (epoch === identityEpoch()) { label.textContent = '本机服务连接异常'; label.className = 'error-text'; } }
}

async function route() {
  if (!getIdentity()) { if (!booting) showLogin(); return; }
  const current = ++revision;
  disposeView?.();
  disposeView = null;
  const [path, rawQuery = ''] = (location.hash.slice(1) || '/ask').split('?');
  const parts = path.split('/').filter(Boolean);
  const name = parts[0] || 'ask';
  const query = new URLSearchParams(rawQuery);
  document.title = `${names[name] || '页面未找到'} · 海纳知识库`;
  document.querySelector('#page-name').textContent = names[name] || '页面未找到';
  document.querySelectorAll('nav a').forEach(link => {
    const selected = link.dataset.page === (name === 'series' ? 'documents' : name);
    link.classList.toggle('active', selected);
    if (selected) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
  });
  if (name === 'search') document.querySelector('#global-query').value = query.get('q') || '';
  if (!allowedPage(name)) { view.replaceChildren(empty('没有权限访问此页面')); return; }
  view.replaceChildren(el('div', {className: 'loading'}, el('span', {className: 'spinner'}), '正在加载…'));
  try {
    let content;
    if (name === 'ask') content = askView(query);
    else if (name === 'overview') content = await overviewView();
    else if (name === 'documents' && parts[1]) content = await detailView(decodeURIComponent(parts[1]), query);
    else if (name === 'documents') content = query.get('view') === 'series' ? await seriesListView(query) : await documentsView();
    else if (name === 'series' && parts[1]) content = await seriesDetailView(decodeURIComponent(parts[1]));
    else if (name === 'review') content = await reviewView();
    else if (name === 'search') content = await searchView(query);
    else if (name === 'graph') content = await graphView(query);
    else if (name === 'timeline') content = await timelineView(query);
    else if (name === 'admin') content = await adminView();
    else if (name === 'mail') content = parts[1] ? await mailDetailView(decodeURIComponent(parts[1])) : await mailView(query);
    else content = empty('页面未找到', '请从左侧导航选择页面。');
    if (current !== revision) { content.dispose?.(); return; }
    view.replaceChildren(content);
    disposeView = content.dispose || null;
    await content.mount?.();
    view.scrollTop = 0;
    const located = view.querySelector('.highlighted');
    if (located) { located.scrollIntoView({block: 'center'}); located.focus({preventScroll: true}); }
  } catch (error) {
    if (current !== revision) return;
    const message = error.status === 404 && (name === 'series' || query.has('series')) ? '对象不存在或无权查看' : error.message;
    view.replaceChildren(el('div', {className: 'page-error'}, empty('暂时无法加载', message),
      el('button', {className: 'button', onclick: route}, '重试')));
  }
}

document.querySelector('#global-search').addEventListener('submit', event => {
  event.preventDefault();
  const query = document.querySelector('#global-query').value.trim();
  if (!query) return;
  const target = `#/search?q=${encodeURIComponent(query)}`;
  if (location.hash === target) route(); else location.hash = target;
});
window.addEventListener('hashchange', route);
window.addEventListener('kb:updated', () => {
  if (!getIdentity()) return;
  health();
  if (/^#\/(overview|documents|series|review|ask|timeline|graph)(\/|\?|$)/.test(location.hash)) route();
});
initUpload();
initFolderUpload();
bootstrap();
