import {el, api, post, docUrl, timeText} from './dom.js';
import {pollJob, jobStages} from './upload.js';
import {entitySubtypes} from './entity_types.js';
import {can} from './auth_state.js';

let notice = null, noticeTimer = null;
window.addEventListener('kb:identity-changed', () => { clearTimeout(noticeTimer); notice?.remove(); notice = null; });

export function refreshReview() {
  window.dispatchEvent(new Event('kb:updated'));
}

function showNotice(text, decision = null) {
  clearTimeout(noticeTimer);
  notice?.remove();
  const current = el('div', {className: 'panel review-notice', role: 'status'}, el('span', {}, text));
  notice = current;
  if (decision) current.append(undoControl(decision));
  document.body.append(current);
  noticeTimer = setTimeout(() => { current.remove(); if (notice === current) notice = null; }, 8000);
}

export function undoControl(decision) {
  if (!can('review')) return el('span', {hidden: true});
  const error = el('div', {className: 'error-text', role: 'alert'});
  const button = el('button', {className: 'button', type: 'button', 'data-decision-id': decision.decision_id}, '撤销');
  const root = el('div', {className: 'review-actions'}, button, error);
  button.addEventListener('click', async () => {
    button.disabled = true;
    button.textContent = '正在撤销…';
    error.replaceChildren();
    try {
      await api('/api/review/decisions/' + encodeURIComponent(decision.decision_id), {method: 'DELETE'});
      window.dispatchEvent(new CustomEvent('kb:decision-removed', {detail: decision}));
      showNotice('已撤销该决定');
      refreshReview();
    } catch (failure) { error.textContent = ['series_create', 'series_rename', 'series_merge', 'document_series'].includes(decision.kind) && failure.status === 404 ? '对象不存在或无权查看' : failure.message; }
    finally { button.disabled = false; button.textContent = '撤销'; }
  });
  return root;
}

export function reviewControls(actions, onChanged = refreshReview) {
  const root = el('div', {className: 'review-actions'});
  if (!can('review')) return root;
  const error = el('div', {className: 'error-text', role: 'alert'});
  const buttons = actions.map(action => {
    const button = el('button', {className: 'button', type: 'button', 'data-kind': action.kind,
      'data-target': action.target, 'data-value': action.value}, action.text);
    button.addEventListener('click', async () => {
      buttons.forEach(item => { item.disabled = true; });
      button.textContent = '正在处理…';
      error.replaceChildren();
      try {
        const {decision} = await post('/api/review/decisions', {
          kind: action.kind, target: action.target, value: action.value ?? null,
        });
        showNotice('已保存：' + decision.label, decision);
        await onChanged(decision);
      } catch (failure) { error.textContent = action.kind === 'relation_reject' && failure.status === 404 ? '对象不存在' : failure.message; }
      finally { buttons.forEach(item => { item.disabled = false; }); button.textContent = action.text; }
    });
    return button;
  });
  root.append(...buttons, error);
  return root;
}

export function eventControls(event, onChanged = refreshReview) {
  const actions = [];
  if (event.verdict === 'review') actions.push({text: '通过', kind: 'event_pass', target: event.id});
  if (event.verdict === 'review' || event.verdict === 'pass') actions.push({text: '驳回', kind: 'event_reject', target: event.id});
  return reviewControls(actions, onChanged);
}

export function documentReviewControls(record, onChanged = refreshReview) {
  const root = el('div', {className: 'document-review-controls'});
  const error = el('div', {className: 'error-text', role: 'alert'});
  const progress = el('div', {className: 'job-progress muted', role: 'status', 'aria-live': 'polite'});
  const dismiss = el('button', {type: 'button', className: 'button'}, '确认无需处理');
  const note = el('textarea', {name: 'note', rows: 3, maxLength: 500, 'aria-label': '确认备注', placeholder: '备注（可空，最多 500 字）'});
  const save = el('button', {type: 'submit', className: 'button primary'}, '保存确认');
  const cancel = el('button', {type: 'button', className: 'button'}, '取消');
  const form = el('form', {className: 'document-review-form', hidden: true},
    el('label', {}, '确认备注', note), el('div', {className: 'review-actions'}, save, cancel));
  const buttons = [dismiss, save, cancel];
  const controller = new AbortController();
  let busy = false;
  const lock = value => { busy = value; buttons.forEach(button => { button.disabled = value; }); note.disabled = value; };
  dismiss.addEventListener('click', () => { form.hidden = false; note.focus(); });
  cancel.addEventListener('click', () => { form.hidden = true; error.textContent = ''; });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    lock(true);
    error.textContent = '';
    try {
      await post(`/api/documents/${encodeURIComponent(record.version_id)}/review`, {action: 'dismiss', note: note.value});
      await onChanged();
    } catch (failure) { error.textContent = failure.message; }
    finally { lock(false); }
  });
  const actions = el('div', {className: 'review-actions'});
  if (['error', 'qc_blocked', 'unsupported'].includes(record.status)) {
    const retry = el('button', {type: 'button', className: 'button primary'}, '重新导入');
    buttons.push(retry);
    retry.addEventListener('click', async () => {
      if (busy) return;
      lock(true);
      form.hidden = true;
      error.textContent = '';
      try {
        const {job_id} = await post(`/api/documents/${encodeURIComponent(record.version_id)}/retry`, {});
        progress.textContent = '排队中';
        const finished = await pollJob(job_id, job => {
          progress.textContent = jobStages[job.stage] || '执行中';
          if (job.error) error.textContent = job.error;
        }, {signal: controller.signal});
        if (finished) await onChanged();
      } catch (failure) { if (!controller.signal.aborted) error.textContent = failure.message; }
      finally { lock(false); }
    });
    actions.append(retry);
  }
  actions.append(dismiss);
  root.append(actions, form, progress, error);
  root.dispose = () => controller.abort();
  return root;
}

export function documentReviewStatus(record, onChanged = refreshReview) {
  const root = el('div', {className: 'panel compact document-review-status'});
  const review = record.review;
  if (review?.action === 'dismissed') {
    const error = el('div', {className: 'error-text', role: 'alert'});
    const undo = el('button', {type: 'button', className: 'button'}, '撤销确认');
    root.append(el('h2', {}, '已确认'), el('p', {className: 'muted'}, timeText(review.at)),
      ...(review.note ? [el('p', {className: 'review-note'}, review.note)] : []), undo, error);
    undo.addEventListener('click', async () => {
      undo.disabled = true;
      error.textContent = '';
      try {
        await post(`/api/documents/${encodeURIComponent(record.version_id)}/review`, {action: 'undo'});
        await onChanged();
      } catch (failure) { error.textContent = failure.message; }
      finally { undo.disabled = false; }
    });
  } else if (review?.action === 'retried') {
    root.append(el('h2', {}, '已重新导入'), el('p', {className: 'muted'}, timeText(review.at)),
      el('a', {href: docUrl(review.retry_version), className: 'text-link'}, `查看新版本 ${review.retry_version} →`));
  }
  if (record.retry_of) root.append(el('p', {}, '重新导入自 ',
    el('a', {href: docUrl(record.retry_of), className: 'text-link'}, record.retry_of)));
  return root;
}

export function subtypeControl(entity, onChanged = refreshReview) {
  if (!can('review')) return el('span', {hidden: true});
  const select = el('select', {name: 'entity_subtype', 'aria-label': '修改子类'},
    el('option', {value: ''}, '未分类'),
    (entitySubtypes[entity.type] || []).map(value => el('option', {value}, value)));
  select.value = entity.subtype || '';
  const button = el('button', {type: 'submit', className: 'button'}, '保存子类');
  const error = el('div', {className: 'error-text', role: 'alert'});
  const form = el('form', {className: 'subtype-form'}, select, button, error);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (button.disabled) return;
    button.disabled = select.disabled = true; error.textContent = '';
    try {
      const {decision} = await post('/api/review/decisions', {kind: 'entity_subtype', target: entity.id, value: select.value});
      showNotice('已保存：' + decision.label, decision);
      await onChanged(decision);
    } catch (failure) { error.textContent = failure.status === 404 ? '对象不存在' : failure.message; }
    finally { button.disabled = select.disabled = false; }
  });
  return form;
}
