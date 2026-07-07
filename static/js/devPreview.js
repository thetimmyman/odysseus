// static/js/devPreview.js — Dev Preview tool: pick an app under the repos root,
// install deps, start its dev server inside the container, and preview it in an
// iframe. Admin-only server-side. Mirrors crewPanel/terminal: a Tools overlay,
// cookie _api, XSS-safe (_esc / textContent), polled status + logs.
//
// The dev server is published on the Framework's LOOPBACK only (127.0.0.1) — NOT
// the LAN — so preview is over an SSH tunnel (this UI shows the command). The
// status poll also reconciles ORPHANS: if the manager lost track of a server but
// one is still listening, the backend reports it "unmanaged" and we surface a
// Stop so the user can always reap it.

let API_BASE = '';
let _open = false;
let _apps = [];
let _selected = null;        // app id
let _previewPort = 3000;
let _proxyPort = 7100;          // admin-gated in-Odysseus preview proxy port
let _statusOpen = false;        // read-only Settings/Security panel toggle
let _detail = {};            // app id -> per-app detail (install cmd, env status)
let _envOpen = false;        // per-app env-key list expanded?
let _envEditing = null;      // key being edited (masked write), '__add__', or null
let _vaultKeys = {};         // key -> vault source (k3s|vaultwarden) for the selected app
let _vaultMapOpen = false;   // vault-map editor panel expanded?
let _poll = null;            // status/log poll timer
let _logKind = 'run';        // which log stream the drawer shows
let _curRun = null;          // last /status running info

function _esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
function _el(id) { return document.getElementById(id); }
function _toast(m) { if (window.uiModule && window.uiModule.showToast) window.uiModule.showToast(m); }
function _err(m) { if (window.uiModule && window.uiModule.showError) window.uiModule.showError(m); else _toast(m); }

async function _api(path, opts) {
  const u = new URL(`${API_BASE}${path}`, window.location.origin);
  const init = Object.assign({ credentials: 'same-origin' }, opts || {});
  const r = await fetch(u, init);
  if (!r.ok) {
    let detail = `${r.status}`;
    try { const j = await r.json(); if (j && j.detail) detail = j.detail; } catch { /* noop */ }
    const e = new Error(detail); e.status = r.status; throw e;
  }
  return r.json();
}
function _post(path, body) {
  return _api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
}
// PUT/DELETE for the masked env writes. The browser auto-attaches Origin +
// Sec-Fetch-Site on these same-origin requests, satisfying the server's CSRF
// guard; credentials:'same-origin' carries the admin cookie.
function _put(path, body) {
  return _api(path, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
}
function _del(path) {
  return _api(path, { method: 'DELETE' });
}

// --- apps rail ---------------------------------------------------------------
async function _loadApps() {
  const list = _el('preview-apps');
  if (!list) return;
  try {
    const j = await _api('/api/dev-preview/apps');
    _apps = j.apps || [];
    _previewPort = j.preview_port || 3000;
    if (j.proxy_port) _proxyPort = j.proxy_port;
    const bp = _el('preview-banner-port');
    if (bp) bp.textContent = _previewPort;
    list.innerHTML = '';
    if (!_apps.length) {
      const e = document.createElement('div');
      e.className = 'crew-empty';
      e.textContent = 'No apps with a package.json found under the repos root.';
      list.appendChild(e);
      return;
    }
    for (const app of _apps) {
      const row = document.createElement('div');
      row.className = 'crew-run-row preview-app-row';
      if (_selected === app.id) row.classList.add('selected');
      row.dataset.appId = app.id;
      const top = document.createElement('div');
      top.className = 'crew-run-top';
      const nm = document.createElement('span');
      nm.className = 'preview-app-name';
      nm.textContent = app.name || app.id;
      top.appendChild(nm);
      if (app.running) {
        const dot = document.createElement('span');
        dot.className = 'crew-badge crew-st-run';
        dot.textContent = ':' + app.port;
        top.appendChild(dot);
      }
      row.appendChild(top);
      const meta = document.createElement('div');
      meta.className = 'preview-app-sub';
      const bits = [];
      if (app.branch) bits.push(app.branch);
      bits.push(app.installed ? 'installed' : 'not installed');
      meta.textContent = bits.join(' · ');
      row.appendChild(meta);
      list.appendChild(row);
    }
    // keep the selection's main panel fresh
    if (_selected) _renderMain();
  } catch (e) {
    if (e.status === 403) { list.innerHTML = '<div class="crew-empty">Dev Preview is admin-only — sign in on the desktop.</div>'; return; }
    list.innerHTML = `<div class="crew-empty">Could not load apps (${_esc(e.message || e)})</div>`;
  }
}

function _selApp() { return _apps.find((a) => a.id === _selected) || null; }

// --- main panel --------------------------------------------------------------
function _renderMain() {
  const app = _selApp();
  const empty = _el('preview-empty');
  const main = _el('preview-main-inner');
  if (!app) { if (empty) empty.style.display = ''; if (main) main.style.display = 'none'; return; }
  if (empty) empty.style.display = 'none';
  if (main) main.style.display = '';

  _el('preview-app-title').textContent = app.name || app.id;
  const meta = _el('preview-app-meta');
  const mbits = [];
  if (app.branch) mbits.push('branch ' + app.branch);
  if (app.remote) mbits.push(app.remote);
  meta.textContent = mbits.join('  ·  ');

  // script selector (dev scripts only)
  const sel = _el('preview-script');
  const wantScripts = (app.dev_scripts && app.dev_scripts.length) ? app.dev_scripts : ['dev'];
  sel.innerHTML = '';
  for (const s of wantScripts) {
    const o = document.createElement('option');
    o.value = s; o.textContent = s;
    sel.appendChild(o);
  }

  const installed = !!app.installed;
  const running = !!app.running;
  _el('preview-install-btn').style.display = installed ? 'none' : '';
  _el('preview-install-btn').disabled = app.install_status === 'running';
  _el('preview-install-btn').textContent = app.install_status === 'running' ? 'Installing…' : 'Install deps';
  _el('preview-start-btn').style.display = (installed && !running) ? '' : 'none';
  _el('preview-stop-btn').style.display = running ? '' : 'none';
  sel.disabled = running || !installed;

  const portEl = _el('preview-port');
  portEl.textContent = running ? (':' + app.port) : (':' + _previewPort);
  portEl.className = 'preview-port' + (running ? ' is-live' : '');

  // The dev server is loopback-only; preview goes through the admin-gated
  // in-Odysseus proxy on _proxyPort (same host, cookie carried cross-port),
  // which strips frame headers so the app renders right here in an iframe.
  const frame = _el('preview-frame');
  const openBtn = _el('preview-open-btn');
  const fe = _el('preview-frame-empty');
  const tunnel = _el('preview-tunnel');
  if (tunnel) tunnel.style.display = 'none';
  const proxyUrl = `${window.location.protocol}//${window.location.hostname}:${_proxyPort}/`;
  const ready = running && app.run_status === 'running';
  if (ready) {
    if (frame.dataset.url !== proxyUrl) { frame.src = proxyUrl; frame.dataset.url = proxyUrl; }
    frame.style.display = '';
    openBtn.style.display = ''; openBtn.href = proxyUrl; openBtn.textContent = 'Open ↗';
    fe.style.display = 'none';
  } else if (running) {
    frame.style.display = 'none'; frame.removeAttribute('src'); frame.dataset.url = '';
    openBtn.style.display = 'none';
    fe.style.display = ''; fe.textContent = 'Starting dev server… compiling (first load can take 10–30s).';
  } else {
    frame.style.display = 'none'; frame.removeAttribute('src'); frame.dataset.url = '';
    openBtn.style.display = 'none';
    fe.style.display = ''; fe.textContent = 'Not running. Install deps (if needed), then Start the dev server.';
  }

  _renderAppConfig();
}

// --- per-app config (read-only: install cmd, start scripts, env status) ------
// Fetches /app/{id} (install command, env-file KEY status — never values) and
// caches it; _renderAppConfig reads the cache so the poll loop stays cheap.
async function _loadAppDetail(id) {
  if (!id) return;
  try {
    _detail[id] = await _api(`/api/dev-preview/app/${encodeURIComponent(id)}`);
  } catch (e) {
    if (e.status !== 403) _detail[id] = { _error: e.message || String(e) };
  }
  if (_selected === id) _renderAppConfig();
}

const _ENV_META = {
  ready:      { dot: 'is-ok',  word: 'ready' },
  partial:    { dot: 'is-warn', word: 'partial' },
  missing:    { dot: 'is-bad', word: 'missing' },
  configured: { dot: 'is-cfg', word: 'configured' },
  none:       { dot: 'is-na',  word: 'no env files' },
};
const _KEY_BADGE = { set: '✓', blank: '○', missing: '✗' };

function _renderAppConfig() {
  const wrap = _el('preview-appcfg');
  if (!wrap) return;
  const d = _selected ? _detail[_selected] : null;
  if (!d || d._error) { wrap.style.display = 'none'; return; }
  wrap.style.display = '';
  _el('preview-install-cmd').textContent = d.install_command || '—';
  _el('preview-appcfg-pm').textContent = d.package_manager || 'npm';

  _vaultKeys = {};
  (d.vault_keys || []).forEach((v) => { _vaultKeys[v.key] = v.source; });
  const _vmc = _el('preview-vaultmap-count');
  if (_vmc) _vmc.textContent = '(' + Object.keys(_vaultKeys).length + ')';

  const env = d.env || {};
  const m = _ENV_META[env.status] || _ENV_META.none;
  const dot = _el('preview-env-dot');
  if (dot) dot.className = 'preview-env-dot ' + m.dot;
  const label = _el('preview-env-label');
  if (label) {
    let txt = 'env ' + m.word;
    if (env.status === 'ready' || env.status === 'partial') txt += ` (${env.set}/${env.total})`;
    label.textContent = txt;
  }
  const caret = _el('preview-env-caret');
  if (caret) caret.innerHTML = _envOpen ? '&#9662;' : '&#9656;';
  const chip = _el('preview-env-chip');
  if (chip) chip.style.display = (env.status === 'none') ? 'none' : '';

  const keysBox = _el('preview-env-keys');
  if (!keysBox) return;
  keysBox.style.display = (_envOpen && env.status !== 'none') ? '' : 'none';
  if (!_envOpen || env.status === 'none') return;
  if (_envEditing) return;   // don't clobber an in-progress masked edit on poll re-render
  keysBox.innerHTML = '';

  const editable = env.gitignored === true;   // backend also refuses unless gitignored

  const head = document.createElement('div');
  head.className = 'preview-env-files';
  const gi = env.gitignored;
  const giTxt = gi === true ? '.env.local · gitignored ✓'
    : gi === false ? '⚠ .env.local is NOT gitignored'
      : '.env.local';
  head.textContent = (env.has_local ? giTxt : 'no .env.local')
    + (env.has_example ? '  ·  .env.example template' : '  ·  no .env.example template');
  if (gi === false) head.classList.add('is-bad');
  keysBox.appendChild(head);

  const note = document.createElement('div');
  note.className = 'preview-env-note';
  note.textContent = 'Key names only — values are never returned, logged, or shown here.';
  keysBox.appendChild(note);

  const warn = document.createElement('div');
  warn.className = 'preview-env-warn' + (editable ? '' : ' is-bad');
  warn.textContent = editable
    ? '⚠ Writing sets real values on disk. Prefer a dev/staging Supabase over production. Values are write-only — sent to the server, never shown back. On a plaintext (HTTP) connection the value crosses the wire; use an SSH tunnel or HTTPS for production credentials.'
    : '⚠ .env.local is not gitignored (or absent) — editing is disabled so a committable secrets file is never written.';
  keysBox.appendChild(warn);

  const list = (env.keys || []);
  if (!list.length && !(env.extra_keys || []).length) {
    const e = document.createElement('div');
    e.className = 'preview-env-empty';
    e.textContent = env.has_example ? 'Template declares no keys.' : 'No template to compare against.';
    keysBox.appendChild(e);
  }
  for (const k of list) keysBox.appendChild(_envKeyRow(k.key, k.status, false, editable));
  for (const k of (env.extra_keys || [])) keysBox.appendChild(_envKeyRow(k, 'set', true, editable));

  if (editable) {
    const addCtl = document.createElement('div');
    addCtl.className = 'preview-env-addctl';
    const addBtn = document.createElement('button');
    addBtn.type = 'button'; addBtn.className = 'preview-env-btn'; addBtn.textContent = '+ Add key';
    addBtn.dataset.act = 'add';
    addCtl.appendChild(addBtn);
    keysBox.appendChild(addCtl);
  }
}

function _envKeyRow(name, status, extra, editable) {
  const row = document.createElement('div');
  row.className = 'preview-env-row env-' + status;
  row.dataset.key = name;
  const b = document.createElement('span');
  b.className = 'preview-env-badge';
  b.textContent = _KEY_BADGE[status] || '—';
  const n = document.createElement('span');
  n.className = 'preview-env-key';
  n.textContent = name;
  row.appendChild(b); row.appendChild(n);
  const tag = document.createElement('span');
  tag.className = 'preview-env-tag';
  tag.textContent = extra ? 'extra' : status;
  row.appendChild(tag);
  if (editable) {
    // Vault-source button: only for mapped keys that aren't set yet. Fetches the
    // value server-side (k3s/Vaultwarden via ssh) — never enters the browser.
    const vsrc = _vaultKeys[name];
    if (vsrc && (status === 'missing' || status === 'blank')) {
      const v = document.createElement('button');
      v.type = 'button'; v.className = 'preview-env-btn preview-env-btn-vault';
      v.textContent = '⤓ ' + vsrc;
      v.title = 'Fetch this value from ' + vsrc + ' (written straight to .env.local, never shown)';
      v.dataset.act = 'source'; v.dataset.key = name;
      row.appendChild(v);
    }
    const setBtn = document.createElement('button');
    setBtn.type = 'button'; setBtn.className = 'preview-env-btn';
    setBtn.textContent = (status === 'set') ? 'Edit' : 'Set';
    setBtn.dataset.act = 'edit'; setBtn.dataset.key = name;
    row.appendChild(setBtn);
    if (status === 'set' || status === 'blank') {
      const clr = document.createElement('button');
      clr.type = 'button'; clr.className = 'preview-env-btn preview-env-btn-clear';
      clr.textContent = 'Clear';
      clr.dataset.act = 'clear'; clr.dataset.key = name;
      row.appendChild(clr);
    }
  }
  return row;
}

async function _envSourceVault(key) {
  const app = _selApp();
  if (!app) return;
  try {
    const r = await _post('/api/dev-preview/app/' + encodeURIComponent(app.id) + '/env/source', { key });
    _toast('Sourced ' + key + ' from ' + (r.source || 'vault'));   // key + source only, no value
    await _loadAppDetail(app.id);
  } catch (e) {
    _err('Source failed: ' + (e.message || e));   // static backend reason, no value
  }
}

// --- vault-map editor (manage key -> {source, locator}; LOCATORS only) --------
async function _toggleVaultMap() {
  _vaultMapOpen = !_vaultMapOpen;
  const panel = _el('preview-vaultmap-panel');
  if (panel) panel.style.display = _vaultMapOpen ? '' : 'none';
  const chip = _el('preview-vaultmap-chip');
  if (chip) chip.classList.toggle('active', _vaultMapOpen);
  const car = _el('preview-vaultmap-caret');
  if (car) car.innerHTML = _vaultMapOpen ? '&#9662;' : '&#9656;';
  if (_vaultMapOpen) await _loadVaultMap();
}

async function _loadVaultMap() {
  const app = _selApp();
  const panel = _el('preview-vaultmap-panel');
  if (!app || !panel) return;
  try {
    const data = await _api('/api/dev-preview/app/' + encodeURIComponent(app.id) + '/vault-map');
    _renderVaultMap(data);
  } catch (e) {
    panel.textContent = 'Could not load mappings (' + (e.message || e) + ')';
  }
}

function _locSummary(m) {
  if (m.source === 'k3s') return (m.ns || '?') + '/' + (m.secret || '?') + ':' + (m.key || '?');
  if (m.source === 'vaultwarden') return (m.item_id || '?') + ' · ' + (m.field || 'password');
  return '';
}

function _renderVaultMap(data) {
  const panel = _el('preview-vaultmap-panel');
  if (!panel) return;
  panel.innerHTML = '';
  const note = document.createElement('div');
  note.className = 'preview-env-note';
  note.textContent = 'Maps an env key to a vault locator — locators only, never secret values. The ⤓ button on a missing/blank key sources it server-side.';
  panel.appendChild(note);

  const map = data.map || {};
  const keys = Object.keys(map).sort();
  if (!keys.length) {
    const e = document.createElement('div'); e.className = 'preview-env-empty';
    e.textContent = 'No mappings yet — add one below.'; panel.appendChild(e);
  }
  for (const k of keys) {
    const m = map[k] || {};
    const row = document.createElement('div'); row.className = 'preview-vm-row';
    const kk = document.createElement('span'); kk.className = 'preview-env-key'; kk.textContent = k;
    const src = document.createElement('span'); src.className = 'preview-vm-src'; src.textContent = m.source;
    const loc = document.createElement('span'); loc.className = 'preview-vm-loc'; loc.textContent = _locSummary(m);
    const rm = document.createElement('button'); rm.type = 'button';
    rm.className = 'preview-env-btn preview-env-btn-clear'; rm.textContent = '✕'; rm.title = 'Remove mapping';
    rm.addEventListener('click', () => _vaultMapRemove(k));
    row.append(kk, src, loc, rm);
    panel.appendChild(row);
  }
  panel.appendChild(_vaultMapAddForm(data.sources || ['k3s', 'vaultwarden']));
}

function _vaultMapAddForm(sources) {
  const form = document.createElement('div'); form.className = 'preview-vm-form';
  const key = document.createElement('input');
  key.type = 'text'; key.placeholder = 'ENV_KEY'; key.className = 'preview-env-keyinput';
  key.autocomplete = 'off'; key.setAttribute('autocapitalize', 'characters');
  const sel = document.createElement('select'); sel.className = 'preview-select';
  for (const s of sources) { const o = document.createElement('option'); o.value = s; o.textContent = s; sel.appendChild(o); }
  const locWrap = document.createElement('span'); locWrap.className = 'preview-vm-loc-inputs';
  const mk = (ph) => { const i = document.createElement('input'); i.type = 'text'; i.placeholder = ph; i.className = 'preview-vm-locinput'; i.autocomplete = 'off'; return i; };
  const k3sNs = mk('namespace'), k3sSecret = mk('secret'), k3sKey = mk('key (defaults to ENV_KEY)');
  const vwItem = mk('item id'), vwField = mk('field (password)');
  const renderLoc = () => {
    locWrap.innerHTML = '';
    if (sel.value === 'k3s') locWrap.append(k3sNs, k3sSecret, k3sKey);
    else locWrap.append(vwItem, vwField);
  };
  sel.addEventListener('change', renderLoc); renderLoc();
  const add = document.createElement('button');
  add.type = 'button'; add.className = 'preview-env-btn preview-env-btn-save'; add.textContent = 'Add';
  add.addEventListener('click', () => {
    const k = (key.value || '').trim();
    const mapping = (sel.value === 'k3s')
      ? { source: 'k3s', ns: k3sNs.value.trim(), secret: k3sSecret.value.trim(), key: (k3sKey.value.trim() || k) }
      : { source: 'vaultwarden', item_id: vwItem.value.trim(), field: vwField.value.trim() || 'password' };
    _vaultMapAdd(k, mapping);
  });
  form.append(key, sel, locWrap, add);
  return form;
}

async function _vaultMapAdd(key, mapping) {
  const app = _selApp();
  if (!app) return;
  if (!key || !_ENV_KEY_RX.test(key)) { _err('Invalid key name'); return; }
  try {
    await _put('/api/dev-preview/app/' + encodeURIComponent(app.id) + '/vault-map', { key, mapping });
    _toast('Mapped ' + key + ' → ' + mapping.source);
    await _loadVaultMap();
    await _loadAppDetail(app.id);   // refresh the per-key ⤓ buttons
  } catch (e) { _err('Add mapping failed: ' + (e.message || e)); }
}

async function _vaultMapRemove(key) {
  const app = _selApp();
  if (!app) return;
  try {
    await _del('/api/dev-preview/app/' + encodeURIComponent(app.id) + '/vault-map/' + encodeURIComponent(key));
    _toast('Removed mapping ' + key);
    await _loadVaultMap();
    await _loadAppDetail(app.id);
  } catch (e) { _err('Remove failed: ' + (e.message || e)); }
}

// --- masked write: inline form, poll-protected (values never echoed) ---------
const _ENV_KEY_RX = /^[A-Za-z_][A-Za-z0-9_]*$/;

function _envBeginEdit(key, isAdd) {
  _envEditing = isAdd ? '__add__' : key;
  const box = _el('preview-env-keys');
  if (!box) return;
  box.querySelectorAll('.preview-env-form').forEach((n) => n.remove());
  const form = document.createElement('div');
  form.className = 'preview-env-form';
  let keyInput = null;
  if (isAdd) {
    keyInput = document.createElement('input');
    keyInput.type = 'text'; keyInput.placeholder = 'KEY_NAME';
    keyInput.className = 'preview-env-keyinput';
    keyInput.autocomplete = 'off'; keyInput.spellcheck = false;
    keyInput.setAttribute('autocapitalize', 'characters');
    form.appendChild(keyInput);
  }
  const val = document.createElement('input');
  // masked, autofill-proofed — the password manager's save prompt is dodged by
  // clearing + removing the node on submit/cancel.
  val.type = 'password'; val.placeholder = 'value (write-only)';
  val.className = 'preview-env-valinput';
  val.autocomplete = 'new-password'; val.spellcheck = false;
  val.setAttribute('autocapitalize', 'off'); val.name = 'dp-env-' + Math.random().toString(36).slice(2);
  form.appendChild(val);
  const save = document.createElement('button');
  save.type = 'button'; save.className = 'preview-env-btn preview-env-btn-save'; save.textContent = 'Save';
  save.addEventListener('click', () => _envSubmit(isAdd ? (keyInput.value || '').trim() : key, val));
  const cancel = document.createElement('button');
  cancel.type = 'button'; cancel.className = 'preview-env-btn'; cancel.textContent = 'Cancel';
  cancel.addEventListener('click', _envCancelEdit);
  form.appendChild(save); form.appendChild(cancel);
  val.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') save.click();
    else if (e.key === 'Escape') _envCancelEdit();
  });
  if (isAdd) {
    const addCtl = box.querySelector('.preview-env-addctl');
    (addCtl || box).appendChild(form);
  } else {
    const row = box.querySelector('.preview-env-row[data-key="' + key + '"]');
    (row || box).appendChild(form);
  }
  (isAdd ? keyInput : val).focus();
}

function _envScrub(inputEl) {
  // best-effort wipe of the value reference from the DOM input
  try { inputEl.value = ''; inputEl.remove(); } catch { /* noop */ }
}

async function _envSubmit(key, valInput) {
  const app = _selApp();
  if (!app) return;
  if (!key || !_ENV_KEY_RX.test(key)) { _err('Invalid key name'); return; }
  const value = valInput.value;
  try {
    await _put('/api/dev-preview/app/' + encodeURIComponent(app.id) + '/env', { key, value });
    _envScrub(valInput);
    _envEditing = null;
    _toast('Saved ' + key);            // key name only — never the value
    await _loadAppDetail(app.id);
  } catch (e) {
    // e.message is the server's STATIC reason (e.g. "value may not contain '$'")
    // — it never contains the value.
    _err('Save failed: ' + (e.message || e));
  }
}

function _envCancelEdit() {
  _envEditing = null;
  const box = _el('preview-env-keys');
  if (box) box.querySelectorAll('.preview-env-form .preview-env-valinput').forEach((i) => { i.value = ''; });
  _renderAppConfig();
}

async function _envClearKey(key) {
  const app = _selApp();
  if (!app) return;
  if (!window.confirm('Clear ' + key + ' from .env.local?')) return;
  try {
    await _del('/api/dev-preview/app/' + encodeURIComponent(app.id) + '/env/' + encodeURIComponent(key));
    _toast('Cleared ' + key);
    await _loadAppDetail(app.id);
  } catch (e) { _err('Clear failed: ' + (e.message || e)); }
}

function _toggleEnvKeys() {
  _envOpen = !_envOpen;
  _renderAppConfig();
}

function _selectApp(id) {
  _selected = id;
  _envOpen = false;
  _envEditing = null;
  document.querySelectorAll('#preview-apps .preview-app-row').forEach((r) => {
    r.classList.toggle('selected', r.dataset.appId === id);
  });
  _logKind = (_selApp() && _selApp().install_status === 'running') ? 'install' : 'run';
  _renderMain();
  _renderAppConfig();
  _loadAppDetail(id);
  _refreshLogs();
}

// --- actions -----------------------------------------------------------------
async function _install() {
  const app = _selApp();
  if (!app) return;
  try {
    await _post('/api/dev-preview/install', { app_id: app.id });
    _logKind = 'install';
    _toast('Installing dependencies…');
    _setLogsOpen(true);
    await _loadApps();
    _loadAppDetail(app.id);
  } catch (e) { _err('Install failed: ' + (e.message || e)); }
}

async function _start() {
  const app = _selApp();
  if (!app) return;
  const script = _el('preview-script').value || 'dev';
  const btn = _el('preview-start-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Starting…'; }
  try {
    await _post('/api/dev-preview/start', { app_id: app.id, script });
    _logKind = 'run';
    _setLogsOpen(true);
    _toast('Starting dev server…');
    await _loadApps();
  } catch (e) {
    _err('Start failed: ' + (e.message || e));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Start dev server'; }
  }
}

async function _stop() {
  const app = _selApp();
  if (!app) return;
  try {
    await _post('/api/dev-preview/stop', { app_id: app.id });
    _toast('Stopping…');
    await _loadApps();
  } catch (e) { _err('Stop failed: ' + (e.message || e)); }
}

// --- logs --------------------------------------------------------------------
function _setLogsOpen(openIt) {
  const d = _el('preview-logs-wrap');
  if (d) d.style.display = openIt ? '' : 'none';
  const t = _el('preview-logs-toggle');
  if (t) t.textContent = openIt ? 'Hide logs' : 'Show logs';
}
function _logsOpen() {
  const d = _el('preview-logs-wrap');
  return d && d.style.display !== 'none';
}
async function _refreshLogs() {
  const app = _selApp();
  const box = _el('preview-logs');
  if (!app || !box || !_logsOpen()) return;
  try {
    const j = await _api(`/api/dev-preview/logs?app_id=${encodeURIComponent(app.id)}&kind=${_logKind}`);
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.textContent = (j.lines || []).join('\n');
    if (atBottom) box.scrollTop = box.scrollHeight;
  } catch { /* noop */ }
}

// --- poll loop ---------------------------------------------------------------
async function _tick() {
  if (!_open) return;
  try {
    const s = await _api('/api/dev-preview/status');
    _curRun = s.running;
    _renderUnmanaged(s.running);
  } catch { /* noop */ }
  // cheap refresh of the rail (install/running flips) + logs
  await _loadApps();
  await _refreshLogs();
}

// An orphaned server (manager lost _running but the port is still live) — give
// the user an authoritative Stop.
function _renderUnmanaged(run) {
  const el = _el('preview-unmanaged');
  if (!el) return;
  el.style.display = (run && run.unmanaged) ? '' : 'none';
}
async function _stopUnmanaged() {
  const btn = _el('preview-unmanaged-stop');
  if (btn) btn.disabled = true;
  try {
    // No app_id → backend kills whatever is listening on the port.
    const r = await _api('/api/dev-preview/stop', { method: 'POST' });
    const n = (r.killed_orphans || []).length;
    _toast('Stopped' + (n ? ` (reaped ${n} orphan${n > 1 ? 's' : ''})` : ''));
    await _tick();
  } catch (e) {
    _err('Stop failed: ' + (e.message || e));
  } finally {
    if (btn) btn.disabled = false;
  }
}

// --- overlay -----------------------------------------------------------------
function _openOverlay() {
  const ov = _el('preview-overlay');
  if (!ov) return;
  ov.style.display = '';
  _open = true;
  _loadApps();
  if (_poll) clearInterval(_poll);
  _poll = setInterval(_tick, 1800);
}
function _closeOverlay() {
  const ov = _el('preview-overlay');
  if (ov) ov.style.display = 'none';
  _open = false;
  if (_poll) { clearInterval(_poll); _poll = null; }
  // leave the dev server running (Stop is explicit); just drop the iframe
  const frame = _el('preview-frame');
  if (frame) { frame.removeAttribute('src'); frame.dataset.url = ''; }
}

// --- read-only Settings + Security Status panel ------------------------------
async function _toggleStatus() {
  _statusOpen = !_statusOpen;
  const panel = _el('preview-status-panel');
  if (panel) panel.style.display = _statusOpen ? '' : 'none';
  const btn = _el('preview-status-btn');
  if (btn) btn.classList.toggle('active', _statusOpen);
  if (_statusOpen) await _loadStatusPanel();
}
async function _loadStatusPanel() {
  try {
    const [sec, cfg] = await Promise.all([
      _api('/api/dev-preview/security-status'),
      _api('/api/dev-preview/config'),
    ]);
    _renderSecurity(sec);
    _renderConfig(cfg);
  } catch (e) {
    const box = _el('preview-security-list');
    if (box) box.textContent = 'Could not load status (' + (e.message || e) + ')';
  }
}
function _secRow(label, ok, note, kind) {
  const div = document.createElement('div');
  // 'configured' items are a deploy-config fact this process can't verify live,
  // so they get a neutral info marker — NOT a green ✓ that overstates proof.
  const cls = kind === 'configured' ? 'is-cfg'
    : (ok === true ? 'is-ok' : ok === false ? 'is-bad' : 'is-na');
  div.className = 'preview-sec-row ' + cls;
  const b = document.createElement('span');
  b.className = 'preview-sec-badge';
  b.textContent = kind === 'configured' ? 'ⓘ' : (ok === true ? '✓' : ok === false ? '✗' : '—');
  const l = document.createElement('span');
  l.className = 'preview-sec-label';
  l.textContent = label;
  div.appendChild(b); div.appendChild(l);
  if (note) {
    const n = document.createElement('span');
    n.className = 'preview-sec-note';
    n.textContent = note;
    div.appendChild(n);
  }
  return div;
}
function _renderSecurity(s) {
  const box = _el('preview-security-list');
  if (!box) return;
  box.innerHTML = '';
  const proof = s._proof || {};
  const row = (key, label, ok, liveNote) => {
    const pk = proof[key] || 'live';
    let note = liveNote;
    if (pk === 'enforced') note = 'enforced in code';
    else if (pk === 'configured') note = 'configured (compose) — not live-verified';
    box.appendChild(_secRow(label, ok, note, pk));
  };
  row('dev_server_loopback_only', 'Dev server loopback-only', s.dev_server_loopback_only,
    s.dev_server_running ? ('live: bind ' + s.dev_port_bind_scope) : 'not running');
  row('dev_port_host_published', 'Dev port not host-published', !s.dev_port_host_published, ':' + s.dev_port);
  row('proxy_admin_gated', 'Proxy admin-cookie gated', s.proxy_admin_gated);
  row('proxy_csrf_guard', 'Proxy CSRF guard (Origin/Fetch-Metadata)', s.proxy_csrf_guard);
  row('frame_strip_only', 'Frame-strip only (XFO + frame-ancestors)', s.frame_strip_only);
  row('service_role_present', 'Service-role key present', s.service_role_present,
    s.service_role_present === null ? 'no app running' : 'live');
}
function _renderConfig(c) {
  const box = _el('preview-config-list');
  if (!box) return;
  box.innerHTML = '';
  const dep = new Set(c._deployment_coupled || []);
  const cpath = new Set(c._container_path_keys || []);
  const _row = (k) => {
    const div = document.createElement('div'); div.className = 'preview-cfg-row';
    const kk = document.createElement('span'); kk.className = 'preview-cfg-k'; kk.textContent = k;
    div.appendChild(kk); return div;
  };

  // enabled -> editable toggle (master kill-switch)
  const er = _row('enabled');
  const sw = document.createElement('input');
  sw.type = 'checkbox'; sw.className = 'preview-cfg-toggle'; sw.checked = !!c.enabled;
  sw.addEventListener('change', () => _cfgSave({ enabled: sw.checked }, sw));
  const eh = document.createElement('span'); eh.className = 'preview-cfg-note';
  eh.textContent = c.enabled ? 'ON' : 'OFF — apps hidden, writes refused';
  er.appendChild(sw); er.appendChild(eh); box.appendChild(er);

  // app_allowlist -> editable text (comma-separated; empty = all)
  const ar = _row('app_allowlist');
  const inp = document.createElement('input');
  inp.type = 'text'; inp.className = 'preview-cfg-input'; inp.autocomplete = 'off';
  inp.placeholder = 'all apps — comma-separate to restrict';
  inp.value = (c.app_allowlist && c.app_allowlist.length) ? c.app_allowlist.join(', ') : '';
  const commit = () => {
    const list = inp.value.split(',').map((s) => s.trim()).filter(Boolean);
    _cfgSave({ app_allowlist: list.length ? list : null }, inp);
  };
  inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); inp.blur(); } });
  inp.addEventListener('blur', commit);
  ar.appendChild(inp); box.appendChild(ar);

  // display-only rows (package_manager + deployment/container-coupled)
  for (const [k, v] of [['package_manager', c.package_manager], ['repos_root', c.repos_root],
    ['dev_port', c.dev_port], ['proxy_port', c.proxy_port]]) {
    const div = _row(k);
    const vv = document.createElement('span'); vv.className = 'preview-cfg-v'; vv.textContent = v;
    div.appendChild(vv);
    if (dep.has(k)) {
      const t = document.createElement('span'); t.className = 'preview-cfg-tag';
      t.textContent = 'deployment'; t.title = 'Needs a Compose change + restart to take effect';
      div.appendChild(t);
    } else if (cpath.has(k)) {
      const t = document.createElement('span'); t.className = 'preview-cfg-note';
      t.textContent = 'container path (host = REPOS_HOST_DIR)'; div.appendChild(t);
    }
    box.appendChild(div);
  }
}

async function _cfgSave(updates, ctl) {
  if (ctl) ctl.disabled = true;
  try {
    const c = await _put('/api/dev-preview/config', { updates });
    _toast('Saved');
    _renderConfig(c);
  } catch (e) {
    _err('Config save failed: ' + (e.message || e));
    await _loadStatusPanel();   // revert UI to server truth
  } finally {
    if (ctl) ctl.disabled = false;
  }
}

function refresh() { /* host-wide tool; nothing per-session */ }

function init(apiBase) {
  API_BASE = apiBase || '';
  _el('tool-preview-btn')?.addEventListener('click', _openOverlay);
  _el('preview-close')?.addEventListener('click', _closeOverlay);
  _el('preview-status-btn')?.addEventListener('click', _toggleStatus);
  _el('preview-env-chip')?.addEventListener('click', _toggleEnvKeys);
  _el('preview-vaultmap-chip')?.addEventListener('click', _toggleVaultMap);
  _el('preview-env-keys')?.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    if (btn.dataset.act === 'edit') _envBeginEdit(btn.dataset.key, false);
    else if (btn.dataset.act === 'clear') _envClearKey(btn.dataset.key);
    else if (btn.dataset.act === 'source') _envSourceVault(btn.dataset.key);
    else if (btn.dataset.act === 'add') _envBeginEdit('', true);
  });
  _el('preview-apps')?.addEventListener('click', (e) => {
    const row = e.target.closest('.preview-app-row');
    if (row) _selectApp(row.dataset.appId);
  });
  _el('preview-install-btn')?.addEventListener('click', _install);
  _el('preview-start-btn')?.addEventListener('click', _start);
  _el('preview-stop-btn')?.addEventListener('click', _stop);
  _el('preview-unmanaged-stop')?.addEventListener('click', _stopUnmanaged);
  _el('preview-logs-toggle')?.addEventListener('click', () => { _setLogsOpen(!_logsOpen()); _refreshLogs(); });
  _el('preview-logs-kind')?.addEventListener('change', (e) => { _logKind = e.target.value; _refreshLogs(); });
  document.addEventListener('keydown', (e) => {
    const ov = _el('preview-overlay');
    if (e.key === 'Escape' && ov && ov.style.display !== 'none') _closeOverlay();
  });
}

const devPreviewModule = { init, refresh };
export default devPreviewModule;
window.devPreviewModule = devPreviewModule;
