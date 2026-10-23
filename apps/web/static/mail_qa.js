import {el, api, badge, timeText} from './dom.js';
import {identityEpoch} from './auth_state.js';

const filterNames = ['sender', 'recipient', 'start', 'end', 'folder', 'attachments'];
const mailLink = (id, text) => el('a', {href: '#/mail/' + encodeURIComponent(id)}, text || '（无主题）');

export function mailAnswerView(result) {
  const card = el('div', {className: 'answer-card mail-answer'},
    el('div', {className: 'answer-heading'}, el('span', {className: 'an-avatar small'}, '安'),
      el('strong', {}, '小安 · 邮件'), badge(result.status)),
    result.status === 'ungrounded' ? el('p', {className: 'warning-banner'}, '以下回答未能在邮件资料中找到出处，请谨慎参考。') : null,
    el('div', {className: 'answer-text'}, result.answer),
    !result.reranked ? el('p', {className: 'muted mail-rerank'}, '未重排') : null);
  if (result.citations.length) card.append(el('div', {className: 'citations'}, el('div', {className: 'section-label'}, '邮件引用'),
    result.citations.map(citation => el('div', {className: 'citation-card'}, el('span', {className: 'ref-number'}, citation.ref),
      el('div', {}, mailLink(citation.mail_id, citation.subject),
        el('blockquote', {className: 'mail-citation-quote'}, citation.quote),
        citation.sent_at ? el('small', {}, timeText(citation.sent_at)) : null)))));
  const sources = el('details', {className: 'sources'}, el('summary', {}, `交给模型的邮件（${result.sources.length}）`));
  sources.append(...result.sources.map(source => el('div', {className: 'source-row'}, el('span', {className: 'ref-number'}, source.ref),
    el('div', {}, mailLink(source.mail_id, source.subject),
      el('small', {}, source.sender || '发件人未标注', ' · ', source.sent_at ? timeText(source.sent_at) : '时间未标注',
        ' · ', source.folder, source.score === null || source.score === undefined ? '' : ` · 重排分数 ${Number(source.score).toFixed(3)}`)))));
  card.append(sources, el('div', {className: 'answer-footer'}, `${result.model || '未调用模型'} · ${Number(result.seconds).toFixed(1)} 秒`,
    result.dropped_citations ? ` · 已过滤 ${result.dropped_citations} 条无效引用` : ''));
  return card;
}

export function mailQaPanel({filters = null} = {}) {
  const controller = new AbortController(), controls = {}, turns = [];
  let pending = false, disposed = false;
  const messages = el('div', {className: 'mail-qa-messages', 'aria-live': 'polite'});
  const question = el('textarea', {name: 'question', rows: 2, maxLength: 500, required: true,
    placeholder: '向小安询问邮件中的事项，例如：项目预算是多少？', 'aria-label': '输入邮件问题'});
  const send = el('button', {className: 'button primary', type: 'submit'}, '问小安');
  const clear = el('button', {className: 'button quiet', type: 'button', disabled: true}, '清空邮件对话');
  const form = el('form', {className: 'mail-qa-form'});
  if (!filters) {
    const field = (name, label, type = 'text') => {
      const input = el('input', {name, type, 'aria-label': label}); controls[name] = input;
      return el('label', {}, label, input);
    };
    controls.attachments = el('select', {name: 'attachments', 'aria-label': '附件'},
      el('option', {value: ''}, '不限'), el('option', {value: '1'}, '有附件'), el('option', {value: '0'}, '无附件'));
    form.append(el('div', {className: 'mail-filters'}, field('sender', '发件人（地址或名字）'),
      field('recipient', '收件人（地址或名字）'), field('start', '开始日期', 'date'), field('end', '结束日期', 'date'),
      field('folder', '文件夹'), el('label', {}, '附件', controls.attachments)));
  }
  form.append(el('label', {className: 'mail-question'}, '邮件问题', question), el('div', {className: 'scan-actions'}, send));
  const element = el('section', {className: 'panel mail-qa-panel'},
    el('div', {className: 'panel-heading'}, el('h2', {}, '问小安 · 邮件'), clear),
    el('p', {className: 'muted'}, filters ? '只根据上方筛选范围内的邮件回答；关键词框不用于问答。' : '只根据所选范围内的邮件回答，文档不参与本次问答。'),
    messages, form);
  function paint() {
    send.disabled = pending; clear.disabled = pending || !turns.length;
    messages.replaceChildren(...turns.map(turn => el('article', {className: 'chat-turn'},
      el('div', {className: 'user-question'}, el('span', {className: 'user-avatar'}, '我'), el('p', {}, turn.question)),
      turn.pending ? el('div', {className: 'thinking'}, el('span', {className: 'spinner'}), '小安正在查阅邮件…')
        : turn.error ? el('p', {className: 'error-banner', role: 'alert'}, turn.error) : mailAnswerView(turn.result))));
  }
  clear.addEventListener('click', () => { turns.length = 0; paint(); question.focus(); });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const text = question.value.trim();
    if (!text || pending || disposed) return;
    const selected = filters ? filters() : Object.fromEntries(filterNames.map(name => [name, controls[name].value]));
    const body = {question: text};
    for (const name of filterNames) if (selected[name]) body[name] = String(selected[name]);
    const epoch = identityEpoch(), turn = {question: text, pending: true};
    turns.push(turn); pending = true; question.value = ''; paint();
    try {
      const result = await api('/api/mail/ask', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body), signal: controller.signal});
      if (!disposed && epoch === identityEpoch()) turn.result = result;
    } catch (error) { if (!disposed && epoch === identityEpoch()) turn.error = error.message; }
    finally {
      if (!disposed && epoch === identityEpoch()) { pending = turn.pending = false; paint(); }
    }
  });
  question.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); form.requestSubmit(); }
  });
  return {element, dispose() { disposed = true; controller.abort(); turns.length = 0; }};
}
