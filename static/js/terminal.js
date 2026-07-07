// static/js/terminal.js — integrated interactive terminal (roadmap Tier 2 #6).
//
// A real xterm.js terminal bridged over a same-origin WebSocket to a NON-root
// PTY on the Framework (the app runs as uid 1000). The server enforces auth +
// admin + Origin allowlist on the handshake BEFORE accepting; this client just
// wires keystrokes <-> PTY bytes and reports resizes.
//
// Mirrors projectFiles.js / gitPanel.js: a desktop-oriented sidebar section
// that rides the existing sidebar overlay on mobile. xterm + fit addon are
// loaded LOCALLY from /static/lib (vendored, pinned 5.5.0 / 0.10.0) — never a
// live CDN dependency.

let API_BASE = '';
let _curSession = null;

let _term = null;       // xterm.js Terminal
let _fit = null;        // FitAddon instance
let _ws = null;         // active WebSocket
let _libsLoading = null; // promise guard so we load xterm assets once
let _connected = false;
let _disposed = false;
let _resizeObs = null;

function _sessionId() {
  const sm = window.sessionModule;
  if (sm && sm.getCurrentSessionId) return sm.getCurrentSessionId();
  return _curSession;
}

function _status(text, cls) {
  const el = document.getElementById('terminal-status');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'terminal-status' + (cls ? ' ' + cls : '');
}

// --- vendored asset loading (local, pinned) ----------------------------------
function _loadCss(href) {
  if (document.querySelector(`link[href="${href}"]`)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = href;
  document.head.appendChild(link);
}

function _loadScript(src) {
  return new Promise((resolve, reject) => {
    const existing = document.querySelector(`script[data-ody-lib="${src}"]`);
    if (existing) {
      if (existing.dataset.loaded === '1') return resolve();
      existing.addEventListener('load', () => resolve());
      existing.addEventListener('error', () => reject(new Error('load ' + src)));
      return;
    }
    const s = document.createElement('script');
    s.src = src;
    s.async = false;             // preserve order: xterm before the fit addon
    s.dataset.odyLib = src;
    s.addEventListener('load', () => { s.dataset.loaded = '1'; resolve(); });
    s.addEventListener('error', () => reject(new Error('load ' + src)));
    document.head.appendChild(s);
  });
}

function _ensureLibs() {
  if (window.Terminal && window.FitAddon) return Promise.resolve();
  if (_libsLoading) return _libsLoading;
  _loadCss(`${API_BASE}/static/lib/xterm.min.css`);
  _libsLoading = _loadScript(`${API_BASE}/static/lib/xterm.min.js`)
    .then(() => _loadScript(`${API_BASE}/static/lib/xterm-addon-fit.min.js`))
    .catch((e) => { _libsLoading = null; throw e; });
  return _libsLoading;
}

// --- terminal lifecycle ------------------------------------------------------
function _ensureTerm() {
  if (_term) return;
  const host = document.getElementById('terminal-xterm');
  if (!host) return;
  // 5.5.0 UMD build exposes window.Terminal; fit addon exposes window.FitAddon.
  _term = new window.Terminal({
    cursorBlink: true,
    convertEol: false,
    scrollback: 5000,
    fontSize: 13,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
    theme: { background: '#1e1e1e', foreground: '#e6e6e6' },
  });
  _fit = new window.FitAddon.FitAddon();
  _term.loadAddon(_fit);
  _term.open(host);
  try { _fit.fit(); } catch (_e) { /* host not laid out yet */ }

  // Keystrokes -> PTY. Only the bytes the human types; the server's shell argv
  // is fixed, so this can never change WHAT runs.
  _term.onData((data) => {
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      _ws.send(JSON.stringify({ type: 'input', data }));
    }
  });

  // Local resize -> bounded TIOCSWINSZ server-side.
  _term.onResize(({ cols, rows }) => {
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      _ws.send(JSON.stringify({ type: 'resize', cols, rows }));
    }
  });

  // Refit when the sidebar/section is resized.
  if (window.ResizeObserver && !_resizeObs) {
    _resizeObs = new ResizeObserver(() => _doFit());
    _resizeObs.observe(host);
  }
}

function _doFit() {
  if (!_term || !_fit) return;
  try {
    _fit.fit();
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      _ws.send(JSON.stringify({ type: 'resize', cols: _term.cols, rows: _term.rows }));
    }
  } catch (_e) { /* not visible yet */ }
}

function _wsUrl() {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  let url = `${proto}//${window.location.host}/ws/terminal`;
  const sid = _sessionId();
  if (sid) url += `?session_id=${encodeURIComponent(sid)}`;
  return url;
}

function _connect() {
  if (_connected || (_ws && _ws.readyState === WebSocket.OPEN)) return;
  _ensureTerm();
  if (!_term) return;
  _status('Connecting…', 'connecting');

  let ws;
  try {
    // Cookies (the odysseus_session) ride along automatically on a same-origin
    // WebSocket; the server validates them on the handshake before accept().
    ws = new WebSocket(_wsUrl());
  } catch (e) {
    _status('Connection failed', 'err');
    return;
  }
  _ws = ws;

  ws.onopen = () => {
    _connected = true;
    _status('Connected', 'ok');
    _doFit();                  // send the real initial size
    if (_term) _term.focus();
    _setBtns(true);
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_e) { return; }
    if (!msg || typeof msg !== 'object') return;
    if (msg.type === 'output') {
      if (_term) _term.write(msg.data);
    } else if (msg.type === 'exit') {
      if (_term) _term.write('\r\n\x1b[90m[process exited]\x1b[0m\r\n');
    } else if (msg.type === 'error') {
      if (_term) _term.write(`\r\n\x1b[31m${msg.msg || 'error'}\x1b[0m\r\n`);
    }
  };
  ws.onclose = (ev) => {
    _connected = false;
    _ws = null;
    _setBtns(false);
    // 1008 = policy violation (auth/origin reject); 1013 = try again (cap).
    if (ev.code === 1008) _status('Not authorized', 'err');
    else if (ev.code === 1013) _status('Too many terminals open', 'err');
    else _status('Disconnected', '');
  };
  ws.onerror = () => { _status('Connection error', 'err'); };
}

function _disconnect() {
  _connected = false;
  if (_ws) {
    try { _ws.close(); } catch (_e) { /* noop */ }
    _ws = null;
  }
  _setBtns(false);
  _status('Disconnected', '');
}

function _setBtns(isConnected) {
  const conn = document.getElementById('terminal-connect-btn');
  const disc = document.getElementById('terminal-disconnect-btn');
  if (conn) conn.style.display = isConnected ? 'none' : '';
  if (disc) disc.style.display = isConnected ? '' : 'none';
}

// --- public API (mirrors projectFiles/gitPanel) ------------------------------
function refresh(sessionId) {
  // The terminal opens from the Tools menu now — just track the active session
  // so the next connect uses its project_root as cwd.
  _curSession = sessionId;
}

async function _onActivate() {
  // Lazy: only fetch the ~290KB xterm bundle when the user actually opens the
  // terminal section, then connect.
  try {
    await _ensureLibs();
  } catch (e) {
    _status('Could not load terminal libraries', 'err');
    return;
  }
  _ensureTerm();
  _doFit();
  if (!_connected) _connect();
}

// --- overlay open/close (opens as a full page from the Tools menu) -----------
function _openOverlay() {
  const ov = document.getElementById('terminal-overlay');
  if (!ov) return;
  ov.style.display = '';
  _onActivate();                 // lazy-load xterm + connect
  setTimeout(() => _doFit(), 60);   // fit once the overlay has laid out
  setTimeout(() => _doFit(), 300);
}
function _closeOverlay() {
  const ov = document.getElementById('terminal-overlay');
  if (ov) ov.style.display = 'none';
  // Keep the PTY/WebSocket alive so reopening resumes; the server idle cap
  // reaps an abandoned terminal. Use the Stop button to disconnect explicitly.
}

function init(apiBase) {
  API_BASE = apiBase || '';

  document.getElementById('tool-terminal-btn')?.addEventListener('click', () => _openOverlay());
  document.getElementById('terminal-close')?.addEventListener('click', () => _closeOverlay());
  document.getElementById('terminal-connect-btn')?.addEventListener('click', () => _onActivate());
  document.getElementById('terminal-disconnect-btn')?.addEventListener('click', () => _disconnect());
  document.addEventListener('keydown', (e) => {
    const ov = document.getElementById('terminal-overlay');
    if (e.key === 'Escape' && ov && ov.style.display !== 'none') _closeOverlay();
  });

  // Refit when the window resizes (debounced via rAF).
  let raf = 0;
  window.addEventListener('resize', () => {
    if (raf) cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => _doFit());
  });

  _setBtns(false);
}

const terminalModule = { init, refresh };
export default terminalModule;
window.terminalModule = terminalModule;
