import {el, api, heading, empty} from './dom.js';
import {crossAnswerView} from './assistant_qa.js';
import {identityEpoch, getIdentity} from './auth_state.js';

const conversationPath = '/api/assistant/conversations';
const modeNames = {all: '全部', documents: '文档', mails: '邮件'};

export function conversationView(query = new URLSearchParams()) {
  const epoch = identityEpoch(), controller = new AbortController();
  let disposed = false, selected = query.get('conversation'), turns = [], title = '新对话';
  let routeHash = location.hash;
  let pending = false, loading = Boolean(selected), detailFailed = false, mode = 'all';
  let detailError = '';
  let summaries = [], total = 0, offset = 0, listLoading = false, listRevision = 0;
  const dialogs = new Set();
  const current = () => !disposed && epoch === identityEpoch() && location.hash === routeHash;
  const request = (path, options = {}) => api(path, {...options, signal: controller.signal});
  const write = (path, method, data) => request(path, {method, headers: {'Content-Type': 'application/json'},
    ...(data === undefined ? {} : {body: JSON.stringify(data)})});
  const page = el('section', {className: 'ask-page conversation-page'});
  const list = el('div', {className: 'conversation-list'});
  const listCount = el('p', {className: 'muted conversation-count', 'aria-live': 'polite'});
  const listError = el('p', {className: 'error-text', role: 'alert'});
  const more = el('button', {type: 'button', className: 'button quiet', onclick: () => loadList()}, '加载更多对话');
  const listRetry = el('button', {type: 'button', className: 'button quiet', hidden: true, onclick: () => loadList(true)}, '重试列表');
  const titleNode = el('h2', {className: 'conversation-title'}, title);
  const rename = el('button', {type: 'button', className: 'button quiet', onclick: () => editTitle()}, '改名');
  const remove = el('button', {type: 'button', className: 'button quiet', onclick: () => confirmDelete()}, '删除');
  const messages = el('div', {className: 'messages'});
  const input = el('textarea', {name: 'question', placeholder: '输入问题，或继续追问…', rows: 2, maxLength: 500, 'aria-label': '输入问题'});
  const submit = el('button', {type: 'submit', className: 'send-button', 'aria-label': '发送问题'}, '↑');
  const modeButtons = {};
  const modes = el('div', {className: 'tabs ask-mode-switch', 'aria-label': '本轮问答资料类型'});
  const choices = ['all', 'documents', ...(getIdentity()?.features?.mail ? ['mails'] : [])];
  function paintControls() {
    submit.disabled = pending || loading || detailFailed || turns.filter(turn => !turn.pending && !turn.error).length >= 200;
    input.disabled = loading || detailFailed;
    rename.hidden = remove.hidden = !selected;
    rename.disabled = remove.disabled = pending || loading || detailFailed;
    for (const [key, button] of Object.entries(modeButtons)) {
      button.classList.toggle('active', key === mode);
      button.setAttribute('aria-pressed', String(key === mode));
    }
  }
  for (const name of choices) {
    modeButtons[name] = el('button', {type: 'button', className: 'tab', onclick: () => { mode = name; paintControls(); }}, modeNames[name]);
    modes.append(modeButtons[name]);
  }
  function paintMessages() {
    messages.replaceChildren();
    if (loading) { messages.append(el('p', {className: 'loading'}, '正在读取对话…')); paintControls(); return; }
    if (detailFailed) {
      messages.append(el('div', {className: 'error-banner', role: 'alert'}, detailError),
        el('button', {type: 'button', className: 'button', onclick: loadConversation}, '重新读取对话'));
      paintControls(); return;
    }
    if (!turns.length) messages.append(empty('今天想了解什么？', '每轮可选择资料类型，继续提问时会结合本次对话理解问题。'));
    for (const turn of turns) {
      // 遮蔽轮次只使用接口返回的固定文字；其他字段即使存在也不展示。
      if (turn.hidden) { messages.append(el('article', {className: 'chat-turn hidden-turn'},
        el('p', {className: 'warning-banner'}, turn.answer))); continue; }
      const node = el('article', {className: 'chat-turn'},
        el('div', {className: 'turn-mode'}, modeNames[turn.mode] || '全部'),
        el('div', {className: 'user-question'}, el('span', {className: 'user-avatar'}, '我'), el('p', {}, turn.question)));
      if (turn.pending) node.append(el('div', {className: 'thinking'}, el('span', {className: 'spinner'}), '小安正在查阅资料…'));
      else if (turn.error) node.append(el('div', {className: 'error-banner', role: 'alert'}, turn.error));
      else {
        if (turn.rewritten) node.append(el('p', {className: 'rewritten-question'}, '理解为：', turn.standalone));
        if (turn.purged) node.append(el('p', {className: 'warning-banner purged-turn'}, turn.answer));
        else node.append(crossAnswerView(turn));
      }
      messages.append(node);
    }
    if (turns.filter(turn => !turn.pending && !turn.error).length >= 200) messages.append(el('p', {className: 'warning-banner'}, '此对话已满 200 轮，请新建对话。'));
    paintControls();
  }
  function paintList() {
    list.replaceChildren(...summaries.map(row => el('button', {type: 'button',
      className: 'conversation-item' + (row.conversation_id === selected ? ' active' : ''),
      'aria-current': row.conversation_id === selected ? 'true' : 'false', onclick: () => select(row.conversation_id)},
      el('span', {}, row.title), el('small', {}, `${row.turns} 轮`))));
    listCount.textContent = `已显示 ${summaries.length} / ${total} 个对话`;
    more.hidden = offset >= total;
    more.disabled = listLoading;
  }
  async function loadList(reset = false) {
    if (!current() || (listLoading && !reset)) return;
    const revision = ++listRevision;
    listLoading = true; listError.textContent = ''; listRetry.hidden = true;
    const start = reset ? 0 : offset;
    paintList();
    try {
      const data = await request(`${conversationPath}?offset=${start}&limit=20`);
      if (!current() || revision !== listRevision) return;
      // 更新排序后可能在不同页遇见同一个摘要；不显示重复项。
      const combined = reset ? data.conversations : [...summaries, ...data.conversations];
      summaries = [...new Map(combined.map(row => [row.conversation_id, row])).values()];
      total = data.total; offset = start + data.conversations.length;
      if (!data.conversations.length) offset = total;
    } catch (error) {
      if (current() && revision === listRevision && error.name !== 'AbortError') {
        listError.textContent = error.message; listRetry.hidden = false;
      }
    } finally {
      if (current() && revision === listRevision) { listLoading = false; paintList(); }
    }
  }
  function select(id) {
    if (!current()) return;
    // 交给路由卸载旧视图；请求取消后即使服务仍在执行，也不会串入新视图。
    const hash = id ? `#/ask?conversation=${encodeURIComponent(id)}` : '#/ask';
    page.dispose();
    if (location.hash === hash) window.dispatchEvent(new Event('hashchange'));
    else location.hash = hash;
  }
  async function loadConversation() {
    loading = true; detailFailed = false; detailError = ''; paintMessages();
    try {
      const data = await request(`${conversationPath}/${encodeURIComponent(selected)}`);
      if (!current()) return;
      turns = data.turns; title = data.title; titleNode.textContent = title;
    } catch (error) {
      if (!current() || error.name === 'AbortError') return;
      detailFailed = true;
      detailError = error.message;
    } finally {
      if (current()) { loading = false; paintMessages(); }
    }
  }
  function dialog(label) {
    const node = el('dialog', {className: 'conversation-dialog', 'aria-label': label});
    node.append(el('h2', {}, label));
    node.addEventListener('close', () => { dialogs.delete(node); node.remove(); });
    dialogs.add(node); document.body.append(node);
    return node;
  }
  function editTitle() {
    if (!current() || !selected || pending || loading) return;
    const id = selected, node = dialog('修改对话标题');
    const field = el('input', {type: 'text', name: 'title', value: title, maxLength: 40, required: true, 'aria-label': '对话标题'});
    const errorText = el('p', {className: 'error-text', role: 'alert'});
    const save = el('button', {type: 'submit', className: 'button'}, '保存');
    const form = el('form', {}, field, errorText, el('div', {className: 'dialog-actions'},
      el('button', {type: 'button', className: 'button quiet', onclick: () => node.close()}, '取消'), save));
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const value = field.value.trim();
      if (!value || save.disabled) return;
      save.disabled = true;
      try {
        const data = await write(`${conversationPath}/${encodeURIComponent(id)}`, 'PATCH', {title: value});
        if (!current()) return;
        title = data.title; titleNode.textContent = title; node.close(); await loadList(true);
      } catch (error) { if (current() && error.name !== 'AbortError') errorText.textContent = error.message; }
      finally { if (current()) save.disabled = false; }
    });
    node.append(form); node.showModal(); field.focus();
  }
  function confirmDelete() {
    if (!current() || !selected || pending || loading) return;
    const id = selected, node = dialog('删除对话');
    const errorText = el('p', {className: 'error-text', role: 'alert'});
    const confirm = el('button', {type: 'button', className: 'button danger', onclick: async () => {
      if (confirm.disabled) return;
      confirm.disabled = true;
      try {
        await write(`${conversationPath}/${encodeURIComponent(id)}`, 'DELETE');
        if (!current()) return;
        node.close(); select(null);
      } catch (error) { if (current() && error.name !== 'AbortError') errorText.textContent = error.message; }
      finally { if (current()) confirm.disabled = false; }
    }}, '永久删除');
    node.append(el('p', {}, '将永久删除对话“', title, '”及全部轮次，此操作不可恢复。'), errorText,
      el('div', {className: 'dialog-actions'}, el('button', {type: 'button', className: 'button quiet', onclick: () => node.close()}, '取消'), confirm));
    node.showModal();
  }
  const form = el('form', {className: 'composer'}, modes, el('div', {className: 'composer-input'}, input, submit),
    el('div', {className: 'composer-help'}, el('span', {}, 'Enter 发送 · Shift + Enter 换行'), el('span', {}, '请结合引用原文核对答案')));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const question = input.value.trim();
    if (!current() || !question || submit.disabled) return;
    const item = {question, mode, pending: true};
    const body = {question, mode, ...(selected ? {conversation_id: selected} : {})};
    turns.push(item); pending = true; input.value = ''; paintMessages();
    messages.lastElementChild?.scrollIntoView({behavior: 'smooth', block: 'start'});
    try {
      const data = await write('/api/assistant/ask', 'POST', body);
      if (!current()) return;
      selected = data.conversation_id;
      routeHash = `#/ask?conversation=${encodeURIComponent(selected)}`;
      window.history.replaceState(null, '', routeHash);
      // 回读整段历史，使已经收回的权限在本次续问后也立即反映到界面。
      await loadConversation(); await loadList(true);
    } catch (error) {
      if (!current() || error.name === 'AbortError') return;
      item.pending = false; item.error = error.message;
    } finally {
      if (current()) { pending = false; paintMessages(); }
    }
  });
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); form.requestSubmit(); }
  });
  page.append(heading('小安检索', '基于知识库资料作答，每条引用均可回到原文核对。'),
    el('div', {className: 'conversation-layout'},
      el('aside', {className: 'conversation-sidebar', 'aria-label': '对话列表'},
        el('button', {type: 'button', className: 'button', onclick: () => select(null)}, '新建对话'),
        listCount, list, listError, listRetry, more),
      el('div', {className: 'conversation-main'}, el('div', {className: 'conversation-heading'}, titleNode,
        el('div', {className: 'conversation-actions'}, rename, remove)), messages, form)));
  page.mount = async () => { await Promise.all([loadList(true), selected ? loadConversation() : Promise.resolve()]); };
  page.dispose = () => {
    disposed = true; controller.abort();
    for (const node of dialogs) { node.close(); node.remove(); }
    dialogs.clear();
  };
  paintMessages(); paintList();
  return page;
}
