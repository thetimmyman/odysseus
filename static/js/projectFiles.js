import { langIcon } from './langIcons.js';

let API_BASE = '';
let _curSession = null;
const _cache = new Map();   // dirPath -> entries[]
const _open = new Set();    // expanded dir paths

const EXT_TO_LANG = {  // small map; matches document.js dropdown langs
  md: 'markdown', markdown: 'markdown', py: 'python', js: 'javascript', mjs: 'javascript',
  ts: 'typescript', jsx: 'javascript', tsx: 'typescript', json: 'json', yaml: 'yaml', yml: 'yaml',
  html: 'html', htm: 'html', css: 'css', sh: 'bash', bash: 'bash', sql: 'sql', txt: 'text',
  toml: 'ini', ini: 'ini', xml: 'xml', go: 'go', rs: 'rust', java: 'java', c: 'c', cpp: 'cpp', h: 'cpp',
};
const langFromName = (n) => EXT_TO_LANG[(n.split('.').pop() || '').toLowerCase()] || 'text';

function _sessionRoot(id) {
  const sm = window.sessionModule;
  const s = sm && sm.getSessions ? sm.getSessions().find((x) => x.id === id) : null;
  return s && s.project_root ? s.project_root : null;
}

async function _fetchTree(path) {
  const u = new URL(`${API_BASE}/api/project-files/tree`, window.location.origin);
  u.searchParams.set('session_id', _curSession);
  if (path) u.searchParams.set('path', path);
  const r = await fetch(u, { credentials: 'same-origin' });
  if (!r.ok) throw new Error(`tree ${r.status}`);
  return r.json();
}

function _row(entry, depth) {
  const el = document.createElement('div');
  el.className = 'pf-row' + (entry.is_dir ? ' is-folder' : '');
  el.setAttribute('role', 'treeitem');
  el.dataset.path = entry.path;          // server realpath; client NEVER concatenates
  el.dataset.dir = entry.is_dir ? '1' : '0';
  el.style.paddingLeft = (depth * 12 + 8) + 'px';
  if (entry.is_dir) {
    const chev = document.createElement('span');
    chev.className = 'pf-chevron';
    el.appendChild(chev);
  } else {
    const ic = document.createElement('span');
    ic.className = 'pf-icon';
    ic.innerHTML = langIcon(langFromName(entry.name), 12, { style: 'opacity:0.65;flex-shrink:0' });
    el.appendChild(ic);
  }
  const label = document.createElement('span');
  label.className = 'pf-name';
  label.textContent = entry.name;        // textContent: XSS-safe for filenames
  el.appendChild(label);
  return el;
}

async function _renderRoot() {
  const tree = document.getElementById('project-files-tree');
  if (!tree) return;
  tree.innerHTML = '';
  try {
    const data = await _fetchTree(null);
    _cache.set(data.dir, data.entries);
    for (const e of data.entries) tree.appendChild(_row(e, 0));
    if (data.truncated) {
      const t = document.createElement('div');
      t.className = 'pf-truncated';
      t.textContent = '(listing truncated)';
      tree.appendChild(t);
    }
  } catch (e) {
    tree.innerHTML = '<div class="pf-error">Could not load files</div>';
  }
}

async function _toggleDir(rowEl) {
  const path = rowEl.dataset.path;
  const depth = Math.round((parseInt(rowEl.style.paddingLeft) - 8) / 12);
  if (_open.has(path)) {                  // collapse: remove descendant rows
    _open.delete(path);
    rowEl.classList.remove('expanded');
    let n = rowEl.nextElementSibling;
    while (n && Math.round((parseInt(n.style.paddingLeft || '8') - 8) / 12) > depth) {
      const nx = n.nextElementSibling; n.remove(); n = nx;
    }
    return;
  }
  _open.add(path);
  rowEl.classList.add('expanded');
  let entries = _cache.get(path);
  if (!entries) {
    try { const d = await _fetchTree(path); entries = d.entries; _cache.set(path, entries); }
    catch { _open.delete(path); rowEl.classList.remove('expanded'); return; }
  }
  let anchor = rowEl;
  for (const e of entries) {
    const child = _row(e, depth + 1);
    anchor.after(child); anchor = child;
  }
}

async function _openFile(path, name) {
  try {
    const u = new URL(`${API_BASE}/api/project-files/read`, window.location.origin);
    u.searchParams.set('session_id', _curSession);
    u.searchParams.set('path', path);
    const r = await fetch(u, { credentials: 'same-origin' });
    if (r.status === 415 || r.status === 413) {
      if (window.uiModule) window.uiModule.showToast(r.status === 413 ? 'File too large' : 'Binary file');
      return;
    }
    if (!r.ok) throw new Error(`read ${r.status}`);
    const j = await r.json();
    window.documentModule.openFileDoc({ path: j.path, name: j.name, content: j.content, language: langFromName(j.name) });
    // On mobile, close the sidebar so the editor sheet is visible immediately.
    if (window.innerWidth <= 768 && window._odyCloseSidebar) window._odyCloseSidebar();
  } catch (e) {
    if (window.uiModule) window.uiModule.showError('Could not open file');
  }
}

function refresh(sessionId) {
  _curSession = sessionId;
  _cache.clear(); _open.clear();
  const sec = document.getElementById('project-files-section');
  if (!sec) return;
  if (!_sessionRoot(sessionId)) { sec.style.display = 'none'; return; }
  sec.style.display = '';
  _renderRoot();
}

function init(apiBase) {
  API_BASE = apiBase || '';
  const tree = document.getElementById('project-files-tree');
  if (tree) tree.addEventListener('click', (e) => {
    const row = e.target.closest('.pf-row');
    if (!row) return;
    if (row.dataset.dir === '1') _toggleDir(row);
    else _openFile(row.dataset.path, row.querySelector('.pf-name')?.textContent || '');
  });
  document.getElementById('pf-refresh-btn')?.addEventListener('click', () => refresh(_curSession));
  document.getElementById('pf-section-title')?.addEventListener('click', () => refresh(_curSession));
}

const projectFilesModule = { init, refresh };
export default projectFilesModule;
window.projectFilesModule = projectFilesModule;
