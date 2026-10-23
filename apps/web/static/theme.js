const root = document.documentElement;
const button = document.querySelector('#theme-toggle');
let theme = 'dark';
try { theme = localStorage.getItem('haina-theme') === 'light' ? 'light' : 'dark'; } catch { /* 存储不可用时使用深色。 */ }
function paint() {
  root.dataset.theme = theme;
  button.textContent = theme === 'dark' ? '浅色' : '深色';
  button.setAttribute('aria-label', theme === 'dark' ? '切换浅色主题' : '切换深色主题');
  window.dispatchEvent(new Event('kb:theme'));
}
button.addEventListener('click', () => {
  theme = theme === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem('haina-theme', theme); } catch { theme = 'dark'; }
  paint();
});
paint();
