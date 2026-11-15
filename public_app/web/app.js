'use strict';
const $ = id => document.getElementById(id);
let token = '', selectedFiles = [], mode = 'file', previewId = null, busy = false;
let query = '', offset = 0, total = 0, searchGeneration = 0, debounce, toastTimer;
const show = (id, visible) => { $(id).hidden = !visible; };
function error(id, text) { $(id).textContent = text; show(id, Boolean(text)); }
function toast(text) { $('toast').textContent = text; show('toast', true); clearTimeout(toastTimer); toastTimer = setTimeout(() => show('toast', false), 3500); }
async function api(path, payload) {
  const response = await fetch(path, payload === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-YunOS-Token': token }, body: JSON.stringify(payload)
  });
  let data;
  try { data = await response.json(); } catch { throw new Error('服务响应异常，请重新打开 YunOS。'); }
  if (!response.ok) throw new Error(data.error || '操作未完成，请重试。');
  return data;
}
function make(tag, className, text) { const el = document.createElement(tag); if (className) el.className = className; if (text !== undefined) el.textContent = text; return el; }
function dateText(value) { return new Date(value).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' }); }
function closeDialog(dialog) {
  if (busy && dialog === $('settings')) { toast('正在处理，请稍候。'); return; }
  dialog.classList.add('closing');
  setTimeout(() => { dialog.close(); dialog.classList.remove('closing'); }, matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 150);
}
function settingsPage(page) {
  if (busy) return;
  for (const id of ['home', 'import', 'data']) show('settings-' + id, page === id);
  show('settings-back', page !== 'home');
  $('settings-subtitle').textContent = page === 'import' ? '/ 资料导入' : page === 'data' ? '/ 本地数据' : '';
  if (!$('settings').open) $('settings').showModal();
}
function invalidatePreview() {
  previewId = null; show('preview', false); show('import-result', false); $('save-import').disabled = true;
  error('import-error', '');
}
function setBusy(value) {
  busy = value;
  for (const id of ['file-tab', 'text-tab', 'dropzone', 'file-input', 'note-title', 'note-body', 'source', 'encoding', 'reset-import', 'check-import']) $(id).disabled = value;
  $('save-import').disabled = value || !previewId;
  $('settings-import').setAttribute('aria-busy', String(value));
}
function chooseMode(next) {
  if (busy) return;
  mode = next; invalidatePreview();
  for (const tab of ['file', 'text']) {
    $(tab + '-tab').classList.toggle('selected', tab === mode);
    $(tab + '-tab').setAttribute('aria-selected', String(tab === mode));
    show(tab + '-panel', tab === mode);
  }
  show('encoding-field', mode === 'file');
  $('source').value = mode === 'file' ? '文件' : '手动记录';
}
function chooseFiles(files) {
  if (busy) return;
  invalidatePreview();
  selectedFiles = Array.from(files);
  $('file-selection').textContent = selectedFiles.length ? `已选择 ${selectedFiles.length} 个文件 · ${selectedFiles.map(f => f.name).join('、')}` : '尚未选择文件';
  if (selectedFiles.length > 50 || selectedFiles.reduce((sum, f) => sum + f.size, 0) > 10 * 1024 * 1024) {
    selectedFiles = []; $('file-input').value = ''; $('file-selection').textContent = '请减少文件后重新选择';
    error('import-error', '每批最多 50 个文件，总大小不超过 10 MB。');
  }
}
function readFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`无法读取 ${file.name}，请重新选择。`));
    reader.onload = () => resolve({ name: file.name, data: String(reader.result).split(',')[1] });
    reader.readAsDataURL(file);
  });
}
async function checkImport() {
  if (busy) return;
  invalidatePreview(); setBusy(true); $('check-import').textContent = '正在检查…';
  try {
    let payload = { source: $('source').value, encoding: $('encoding').value };
    if (mode === 'file') {
      if (!selectedFiles.length) throw new Error('请先选择文件，或将文件拖入上方框中。');
      if (selectedFiles.some(f => f.size > 5 * 1024 * 1024)) throw new Error('单个文件不能超过 5 MB，请拆分后再导入。');
      payload.files = await Promise.all(selectedFiles.map(readFile));
    } else {
      if (!$('note-body').value.trim()) throw new Error('请先输入或粘贴正文。');
      if (new TextEncoder().encode($('note-body').value).length > 5 * 1024 * 1024) throw new Error('正文不能超过 5 MB。');
      payload.text = $('note-body').value; payload.title = $('note-title').value;
    }
    const result = await api('/api/preview', payload);
    $('preview-rows').replaceChildren();
    for (const row of result.rows) {
      const card = make('div', 'preview-row ' + row.status);
      card.append(make('strong', '', row.title || row.name));
      if (row.excerpt) card.append(make('p', '', row.excerpt + (row.characters > 500 ? '…' : '')));
      card.append(make('small', '', row.detail + (row.encoding ? ` · ${row.characters.toLocaleString()} 字符 · ${row.encoding}` : '')));
      $('preview-rows').append(card);
    }
    const counts = status => result.rows.filter(r => r.status === status).length;
    $('preview-summary').textContent = `${counts('ready')} 个新资料 · ${counts('duplicate')} 个重复 · ${counts('error')} 个无法导入`;
    previewId = result.rows.some(r => r.status !== 'error') ? result.preview_id : null;
    show('preview', true);
    $('preview').scrollIntoView({ block: 'nearest', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
  } catch (exc) { error('import-error', exc.message); }
  finally { setBusy(false); $('check-import').textContent = '检查并预览'; }
}
async function saveImport() {
  if (!previewId || busy) return;
  setBusy(true); $('save-import').textContent = '正在保存…';
  try {
    const result = await api('/api/import', { preview_id: previewId });
    previewId = null;
    $('import-result').textContent = `已保存 ${result.added} 份新资料，跳过 ${result.duplicates} 份重复正文，${result.failed} 份未导入。重复资料的来源已保留。`;
    show('import-result', true); show('preview', false); toast('资料库已更新');
    await refreshCount(); await loadItems(true);
  } catch (exc) { error('import-error', exc.message); }
  finally { setBusy(false); $('save-import').textContent = '确认导入'; }
}
async function refreshCount() {
  const session = await api('/api/session'); token = session.token;
  $('nav-count').textContent = session.count; $('version').textContent = 'v' + session.version; $('data-path').textContent = session.data_directory;
}
async function loadItems(reset = true) {
  const generation = ++searchGeneration;
  if (reset) offset = 0;
  const start = offset;
  error('main-error', ''); $('load-more').disabled = true;
  try {
    const result = await api(`/api/items?q=${encodeURIComponent(query)}&offset=${start}`);
    if (generation !== searchGeneration) return;
    if (reset) $('items').replaceChildren();
    total = result.total;
    for (const item of result.items) {
      const card = make('button', 'item-card');
      card.append(make('h3', '', item.title), make('p', '', item.excerpt));
      const meta = make('div', 'card-meta');
      meta.append(make('span', 'source-pill', [...new Set(item.sources.map(s => s.label))].join(' · ') || '文字'),
                  make('span', '', item.characters.toLocaleString() + ' 字符'), make('time', '', dateText(item.created_at)));
      card.append(meta); card.addEventListener('click', () => readItem(item.id)); $('items').append(card);
    }
    offset = start + result.items.length;
    $('list-title').textContent = query ? '搜索结果' : '最近保存';
    $('result-count').textContent = total + ' 份资料';
    show('empty', total === 0); show('load-more', offset < total);
    $('empty-title').textContent = query ? '暂时没有找到' : '从第一份资料开始';
    $('empty-copy').textContent = query ? '试试更短的关键词，或换一种说法。' : '在「设置 → 资料导入」中，粘贴文字或拖入文件。';
    show('first-import', !query);
  } catch (exc) { if (generation === searchGeneration) error('main-error', exc.message); }
  finally { if (generation === searchGeneration) $('load-more').disabled = false; }
}
let readerGeneration = 0;
async function readItem(id) {
  const generation = ++readerGeneration;
  try {
    const item = await api('/api/items/' + id);
    if (generation !== readerGeneration) return;
    $('reader-title').textContent = item.title; $('reader-body').textContent = item.body;
    $('reader-meta').textContent = item.sources.map(s => s.label + (s.filename ? ' · ' + s.filename : '')).join(' / ') + ' · 保存于 ' + new Date(item.created_at).toLocaleString('zh-CN');
    if (!$('reader').open) $('reader').showModal();
  } catch (exc) { toast(exc.message); }
}
$('settings-button').onclick = () => settingsPage('home');
$('settings-back').onclick = () => settingsPage('home');
$('settings-close').onclick = () => closeDialog($('settings'));
$('reader-close').onclick = () => closeDialog($('reader'));
$('import-menu').onclick = () => settingsPage('import');
$('data-menu').onclick = () => settingsPage('data');
$('first-import').onclick = () => settingsPage('import');
for (const dialog of [$('settings'), $('reader')]) {
  dialog.addEventListener('cancel', event => { event.preventDefault(); closeDialog(dialog); });
  dialog.addEventListener('click', event => {
    const r = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom)) closeDialog(dialog);
  });
}
$('file-tab').onclick = () => chooseMode('file'); $('text-tab').onclick = () => chooseMode('text');
for (const id of ['file-tab', 'text-tab']) $(id).addEventListener('keydown', event => {
  if (['ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); chooseMode(mode === 'file' ? 'text' : 'file'); $(mode + '-tab').focus(); }
});
$('dropzone').onclick = () => $('file-input').click();
$('file-input').onchange = event => { chooseFiles(event.target.files); event.target.value = ''; };
let dragDepth = 0;
$('dropzone').addEventListener('dragenter', event => { event.preventDefault(); if (!busy) { dragDepth++; $('dropzone').classList.add('dragging'); } });
$('dropzone').addEventListener('dragover', event => { event.preventDefault(); event.dataTransfer.dropEffect = busy ? 'none' : 'copy'; });
$('dropzone').addEventListener('dragleave', event => { event.preventDefault(); if (--dragDepth <= 0) $('dropzone').classList.remove('dragging'); });
$('dropzone').addEventListener('drop', event => { event.preventDefault(); dragDepth = 0; $('dropzone').classList.remove('dragging'); chooseFiles(event.dataTransfer.files); });
document.addEventListener('dragover', event => event.preventDefault());
document.addEventListener('drop', event => event.preventDefault());
for (const id of ['note-title', 'note-body', 'source', 'encoding']) $(id).addEventListener('input', invalidatePreview);
$('reset-import').onclick = () => { chooseFiles([]); $('note-title').value = ''; $('note-body').value = ''; $('file-input').value = ''; invalidatePreview(); };
$('check-import').onclick = checkImport; $('save-import').onclick = saveImport;
$('search-form').onsubmit = event => { event.preventDefault(); clearTimeout(debounce); query = $('search').value.trim(); loadItems(true); };
$('search').oninput = () => { show('clear-search', Boolean($('search').value)); clearTimeout(debounce); searchGeneration++; debounce = setTimeout(() => { query = $('search').value.trim(); loadItems(true); }, 180); };
$('clear-search').onclick = () => { clearTimeout(debounce); $('search').value = ''; query = ''; show('clear-search', false); loadItems(true); $('search').focus(); };
$('library-nav').onclick = () => $('clear-search').click();
$('load-more').onclick = () => loadItems(false);
document.addEventListener('keydown', event => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k' && !$('settings').open && !$('reader').open) { event.preventDefault(); $('search').focus(); } });
$('shutdown').onclick = async () => {
  try { await api('/api/shutdown', {}); $('settings').close(); $('items').replaceChildren(); show('empty', false); show('load-more', false); $('search').disabled = true; $('settings-button').disabled = true; error('main-error', 'YunOS 已退出。可以关闭本页面，下次双击 BAT 再次打开。'); }
  catch (exc) { toast(exc.message); }
};
(async () => { try { await refreshCount(); await loadItems(); } catch (exc) { error('main-error', '服务未连接，请重新双击 BAT 启动。'); } })();
