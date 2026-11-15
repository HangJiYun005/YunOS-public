const openButton = document.querySelector('#open');
function render(state) {
  document.querySelector('#status').textContent = state.message;
  document.querySelector('#dot').className = `dot ${state.phase}`;
  openButton.disabled = state.phase === 'starting';
  openButton.textContent = state.phase === 'ready' ? '打开 YunOS  →' : state.phase === 'error' ? '重试并打开' : '正在准备…';
}
window.launcher.onState(render);
window.launcher.state().then(render);
openButton.addEventListener('click', () => window.launcher.open());
document.querySelector('#retry').addEventListener('click', () => window.launcher.retry().then(render));
document.querySelector('#logs').addEventListener('click', () => window.launcher.logs());
