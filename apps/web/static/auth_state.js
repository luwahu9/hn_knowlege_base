// 身份仅保存在当前页面内存中；切换账号时撤销旧请求并通知视图清理。
let identity = null;
let epoch = 0;
let controller = new AbortController();

export const getIdentity = () => identity;
export const identityEpoch = () => epoch;
export const sessionSignal = () => controller.signal;
export function setIdentity(value) {
  controller.abort();
  controller = new AbortController();
  epoch++;
  identity = value;
  window.dispatchEvent(new Event('kb:identity-changed'));
}
export function unauthorized(message = '请先登录') {
  if (identity !== null) setIdentity(null);
  window.dispatchEvent(new CustomEvent('kb:unauthorized', {detail: message}));
}
export const can = action => Boolean(identity?.actions?.includes(action));
export function allowedPage(name) {
  if (name === 'mail') return Boolean(identity?.features?.mail && can('read'));
  if (name === 'admin') return Boolean(identity?.auth_enabled && (can('manage_users') || can('manage_acl')));
  return can(({review: 'review', graph: 'knowledge', timeline: 'knowledge'})[name] || 'read');
}
export function requestOptions(options = {}) {
  const headers = new Headers(options.headers);
  if ((options.method || 'GET').toUpperCase() !== 'GET') headers.set('X-Haina-Request', '1');
  return {...options, headers, signal: options.signal
    ? AbortSignal.any([options.signal, controller.signal]) : controller.signal};
}
export function requireEpoch(expected) {
  if (expected !== epoch) throw new DOMException('登录状态已变化，请重新选择文件。', 'AbortError');
}
