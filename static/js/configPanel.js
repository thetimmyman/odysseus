// static/js/configPanel.js — Settings: the app's configuration home.
//
// PR-A slice: a dedicated Settings overlay reusing the crew-modal / admin-tabs /
// harness-* classes exactly like routingHarness.js, with a fully-working versioned
// Budget editor (structured caps + inline validation + reusable dirty-state +
// confirm-on-publish + version history + rollback + read-only spend cards) and a
// read-only Effective-config view. UI slice over routes/config_routes.py.
//
// Same-origin cookie fetch, display-side admin gating (every /api/config route
// enforces the admin cookie server-side; on 401/403 each panel shows ONE inline
// "Admin session required" state instead of crashing), and XSS-safe rendering
// (textContent / _esc only). The dirty-state helper (createDirtyState) is built
// here as the reusable, app-wide bit the codebase was missing.
//
// This module self-initialises (deferred module scripts run after DOM parse) and
// also exposes window.configPanelModule for parity with the other tool modules.

let API_BASE = '';
let _wired = false;
let _open = false;
let _tab = 'budget';
let _loaded = {};            // tab -> has loaded at least once this session

// Budget tab state --------------------------------------------------------------
let _server = null;          // last GET /api/config/budget payload (server truth)
let _buffer = {};            // field -> raw input string (the edited buffer)
let _budgetDirty = null;     // dirty-state tracker (createDirtyState)

// --- XSS-safe + fetch helpers (mirrors routingHarness.js) -----------------------
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
    let raw = null;
    try { const j = await r.json(); if (j && j.detail != null) { raw = j.detail; detail = j.detail; } } catch { /* noop */ }
    // The CONTRACT returns 400 {detail:[reason,...]}; keep the list intact on the
    // error so the publish/rollback handlers can render each reason on its own line.
    const e = new Error(Array.isArray(detail) ? detail.join('; ') : String(detail));
    e.status = r.status;
    e.detail = raw;
    throw e;
  }
  return r.json();
}
function _post(path, body) {
  return _api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
}

// --- shared render helpers ------------------------------------------------------
function _isAuthErr(e) { return e && (e.status === 401 || e.status === 403); }

function _gate(tab, e) {
  if (!_isAuthErr(e)) return false;
  const panel = document.querySelector(`#config-overlay .harness-panel[data-cfgpanel="${tab}"]`);
  if (!panel) return true;
  const gate = panel.querySelector('.harness-gate');
  const content = panel.querySelector('.harness-panel-content');
  if (gate) gate.style.display = '';
  if (content) content.style.display = 'none';
  return true;
}
function _ungate(tab) {
  const panel = document.querySelector(`#config-overlay .harness-panel[data-cfgpanel="${tab}"]`);
  if (!panel) return;
  const gate = panel.querySelector('.harness-gate');
  const content = panel.querySelector('.harness-panel-content');
  if (gate) gate.style.display = 'none';
  if (content) content.style.display = '';
}

function _fmtTs(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function _fmtUsd(v, digits) {
  if (v == null) return '—';
  const n = Number(v);
  if (isNaN(n)) return String(v);
  return '$' + n.toFixed(digits == null ? 2 : digits);
}
function _badge(text, kind) {
  const b = document.createElement('span');
  b.className = 'crew-badge ' + (kind || 'crew-st-stop');
  b.textContent = text;
  return b;
}
function _tag(text, accent) {
  const t = document.createElement('span');
  t.className = 'harness-tag' + (accent ? ' harness-tag-accent' : '');
  t.textContent = text;
  return t;
}
function _td(content, cls) {
  const td = document.createElement('td');
  if (cls) td.className = cls;
  if (content instanceof Node) td.appendChild(content);
  else td.textContent = content == null ? '—' : String(content);
  return td;
}

// --- reusable dirty-state (the app-wide gap this PR fills) -----------------------
// A tiny, dependency-free tracker: flips a flag element on/off and offers a
// confirm-on-discard gate. Any future Settings tab (or panel) can reuse it —
// it knows nothing about budgets.
function createDirtyState(opts) {
  opts = opts || {};
  let dirty = false;
  return {
    isDirty() { return dirty; },
    set(v) {
      v = !!v;
      if (v === dirty) return;
      dirty = v;
      if (opts.flagEl) opts.flagEl.style.display = v ? '' : 'none';
      if (typeof opts.onChange === 'function') opts.onChange(v);
    },
    // Returns true if it is safe to proceed (not dirty, or the user confirmed).
    confirmDiscard(msg) {
      if (!dirty) return true;
      return window.confirm(msg || 'You have unsaved changes. Discard them?');
    },
  };
}

// --- Budget: caps definition ----------------------------------------------------
// order + labels + captions; ENFORCED=false marks the advisory monthly cap.
const _CAPS = [
  { field: 'daily_max_usd', label: 'Daily', caption: 'General daily spend cap — a hard block; never per-run overridable.' },
  { field: 'weekly_max_usd', label: 'Weekly', caption: 'General weekly spend cap — a hard block; never per-run overridable.' },
  { field: 'monthly_max_usd', label: 'Monthly', advisory: true, caption: 'Advisory only — tracked and reported, but not enforced.' },
  { field: 'premium_daily_max_usd', label: 'Premium daily', caption: 'Premium-model daily cap. Must be ≤ the general daily cap.' },
  { field: 'premium_weekly_max_usd', label: 'Premium weekly', caption: 'Premium-model weekly cap. Must be ≤ the general weekly cap.' },
];

// Parse a raw input string to a number, or NaN when blank/non-numeric.
function _num(raw) {
  const s = String(raw == null ? '' : raw).trim();
  if (s === '') return NaN;
  return Number(s);
}

// Mirror the server-side validate_budget contract on the client for instant
// feedback (the server remains the authority — a rejected publish never writes).
// Returns { reasons:[...], badFields:Set }.
function _validateBuffer() {
  const reasons = [];
  const badFields = new Set();
  const vals = {};
  for (const c of _CAPS) {
    const n = _num(_buffer[c.field]);
    vals[c.field] = n;
    if (!isFinite(n) || n <= 0) {
      reasons.push(`${c.label} must be a positive number.`);
      badFields.add(c.field);
    }
  }
  if (isFinite(vals.premium_daily_max_usd) && isFinite(vals.daily_max_usd)
      && vals.premium_daily_max_usd > vals.daily_max_usd) {
    reasons.push('Premium daily cap must be ≤ the general daily cap.');
    badFields.add('premium_daily_max_usd');
  }
  if (isFinite(vals.premium_weekly_max_usd) && isFinite(vals.weekly_max_usd)
      && vals.premium_weekly_max_usd > vals.weekly_max_usd) {
    reasons.push('Premium weekly cap must be ≤ the general weekly cap.');
    badFields.add('premium_weekly_max_usd');
  }
  return { reasons, badFields };
}

// Has the buffer diverged from the server's caps? (drives the dirty flag)
function _bufferChanged() {
  if (!_server || !_server.caps) return false;
  for (const c of _CAPS) {
    const b = _num(_buffer[c.field]);
    const s = Number(_server.caps[c.field]);
    // Compare numerically when both parse; otherwise compare raw strings so a
    // half-typed value still reads as dirty.
    if (isFinite(b) && isFinite(s)) { if (b !== s) return true; }
    else if (String(_buffer[c.field] ?? '') !== String(_server.caps[c.field] ?? '')) return true;
  }
  return false;
}

// Re-run validation + dirty detection and reflect it in the UI. Called on every
// keystroke and after any (re)load.
function _refreshBudgetState() {
  const { reasons, badFields } = _validateBuffer();
  // per-field highlight + caption state
  for (const c of _CAPS) {
    const card = document.querySelector(`#cfg-budget-caps .cfg-cap[data-cap-field="${c.field}"]`);
    if (!card) continue;
    const bad = badFields.has(c.field);
    const changed = _server && _server.caps
      && String(_buffer[c.field] ?? '') !== String(_server.caps[c.field] ?? '');
    card.classList.toggle('is-bad', bad);
    card.classList.toggle('is-dirty', !bad && !!changed);
  }
  // aggregate warning box
  const warn = _el('cfg-budget-warn');
  if (warn) {
    if (reasons.length) {
      warn.textContent = reasons.join(' ');
      warn.style.display = '';
    } else {
      warn.textContent = '';
      warn.style.display = 'none';
    }
  }
  const changed = _bufferChanged();
  if (_budgetDirty) _budgetDirty.set(changed);
  const pub = _el('cfg-budget-publish');
  if (pub) pub.disabled = !!reasons.length || !changed;
  const rev = _el('cfg-budget-revert');
  if (rev) rev.disabled = !changed;
}

function _renderCaps() {
  const box = _el('cfg-budget-caps');
  if (!box) return;
  box.innerHTML = '';
  for (const c of _CAPS) {
    const card = document.createElement('div');
    card.className = 'cfg-cap';
    card.dataset.capField = c.field;

    const label = document.createElement('div');
    label.className = 'cfg-cap-label';
    label.appendChild(document.createTextNode(c.label));
    if (c.advisory) {
      const adv = document.createElement('span');
      adv.className = 'cfg-cap-advisory';
      adv.textContent = 'advisory — not enforced';
      label.appendChild(adv);
    }

    const wrap = document.createElement('div');
    wrap.className = 'cfg-cap-inputwrap';
    const inp = document.createElement('input');
    inp.type = 'number';
    inp.min = '0';
    inp.step = '0.01';
    inp.autocomplete = 'off';
    inp.spellcheck = false;
    inp.className = 'preview-env-keyinput cfg-cap-input';
    inp.dataset.capField = c.field;
    inp.value = _buffer[c.field] == null ? '' : String(_buffer[c.field]);
    inp.addEventListener('input', () => { _buffer[c.field] = inp.value; _refreshBudgetState(); });
    wrap.appendChild(inp);

    const cap = document.createElement('div');
    cap.className = 'cfg-cap-caption';
    cap.textContent = c.caption;

    card.append(label, wrap, cap);
    box.appendChild(card);
  }
}

function _renderSpend() {
  const box = _el('cfg-budget-spend');
  if (!box) return;
  box.innerHTML = '';
  const caps = (_server && _server.caps) || {};
  const spend = (_server && _server.spend) || {};
  const cards = [
    { key: 'daily', label: 'daily', spendF: 'daily_usd', capF: 'daily_max_usd', pSpendF: 'premium_daily_usd', pCapF: 'premium_daily_max_usd' },
    { key: 'weekly', label: 'weekly', spendF: 'weekly_usd', capF: 'weekly_max_usd', pSpendF: 'premium_weekly_usd', pCapF: 'premium_weekly_max_usd' },
    { key: 'monthly', label: 'monthly', spendF: 'monthly_usd', capF: 'monthly_max_usd', advisory: true },
  ];
  for (const cd of cards) {
    const spendV = spend[cd.spendF];
    const capV = caps[cd.capF];
    const card = document.createElement('div');
    card.className = 'admin-card harness-stat';
    if (!cd.advisory && capV != null && spendV != null && Number(spendV) >= Number(capV)) card.classList.add('is-over');
    const k = document.createElement('div');
    k.className = 'harness-stat-k';
    k.textContent = cd.label;
    const v = document.createElement('div');
    v.className = 'harness-stat-v';
    v.textContent = `${_fmtUsd(spendV)} / ${capV == null ? 'no cap' : _fmtUsd(capV)}`;
    const sub = document.createElement('div');
    sub.className = 'harness-stat-sub';
    if (cd.advisory) {
      sub.textContent = 'advisory — not enforced';
    } else {
      const pS = spend[cd.pSpendF];
      const pC = caps[cd.pCapF];
      sub.textContent = `premium ${_fmtUsd(pS)} / ${pC == null ? 'no cap' : _fmtUsd(pC)}`;
    }
    card.append(k, v, sub);
    box.appendChild(card);
  }
}

function _renderVersionChip() {
  const chips = _el('cfg-budget-chips');
  if (!chips) return;
  chips.innerHTML = '';
  const v = _server && _server.version;
  // Server-owned + auto-bumped on publish; display only (never editable here).
  chips.appendChild(_tag(`version ${v == null ? '—' : v}`, true));
}

function _renderVersions(versions) {
  const box = _el('cfg-budget-versions');
  if (!box) return;
  box.innerHTML = '';
  if (!versions || !versions.length) {
    const e = document.createElement('div');
    e.className = 'crew-empty';
    e.textContent = 'No archived versions yet — the first publish archives the outgoing file.';
    box.appendChild(e);
    return;
  }
  for (const v of versions) {
    const row = document.createElement('div');
    row.className = 'harness-ver-row';
    const name = document.createElement('span');
    name.className = 'harness-ver-name';
    name.textContent = v.archive_name;
    const meta = document.createElement('span');
    meta.className = 'harness-ver-meta';
    meta.textContent = `v${v.version} · ${_fmtTs(v.ts)} · ${v.actor || '?'}`;
    const rb = document.createElement('button');
    rb.type = 'button';
    rb.className = 'preview-env-btn';
    rb.textContent = 'Rollback';
    rb.dataset.archive = v.archive_name;
    row.append(name, meta, rb);
    box.appendChild(row);
  }
}

// GET /api/config/budget (+ /versions). `force` bypasses the unsaved-buffer guard.
async function _loadBudget(force) {
  // Guard the open-time / explicit refetch against clobbering an unsaved buffer.
  if (!force && _budgetDirty && _budgetDirty.isDirty()) {
    if (!_budgetDirty.confirmDiscard('Reload will discard your unsaved budget changes. Continue?')) return;
  }
  let cur, vers;
  try {
    [cur, vers] = await Promise.all([
      _api('/api/config/budget'),
      _api('/api/config/budget/versions'),
    ]);
  } catch (e) {
    if (_gate('budget', e)) return;
    _err('Could not load budget config: ' + (e.message || e));
    return;
  }
  _ungate('budget');
  _server = cur || {};
  _buffer = {};
  for (const c of _CAPS) {
    const val = _server.caps ? _server.caps[c.field] : undefined;
    _buffer[c.field] = val == null ? '' : String(val);
  }
  _renderCaps();
  _renderSpend();
  _renderVersionChip();
  _renderVersions(vers || []);
  if (_budgetDirty) _budgetDirty.set(false);
  _refreshBudgetState();
}

function _revertBudget() {
  if (!_server || !_server.caps) return;
  for (const c of _CAPS) {
    const val = _server.caps[c.field];
    _buffer[c.field] = val == null ? '' : String(val);
  }
  _renderCaps();
  _refreshBudgetState();
}

async function _publishBudget() {
  const { reasons } = _validateBuffer();
  if (reasons.length) { _refreshBudgetState(); return; }
  if (!window.confirm(
    'Publish these budget caps?\n\n'
    + 'Raising a cap increases spend exposure. The general daily/weekly caps are '
    + 'never per-run overridable. The outgoing version is archived and the change '
    + 'is logged.')) return;
  // Body = the 5 cap floats only. The server owns + auto-bumps the version; we
  // never send a client-supplied version.
  const body = {};
  for (const c of _CAPS) body[c.field] = _num(_buffer[c.field]);
  const btn = _el('cfg-budget-publish');
  if (btn) btn.disabled = true;
  try {
    await _post('/api/config/budget/publish', body);
    _toast('Budget caps published');
    if (_budgetDirty) _budgetDirty.set(false);
    await _loadBudget(true);   // refresh version + spend + versions from server truth
  } catch (e) {
    if (_gate('budget', e)) return;
    const warn = _el('cfg-budget-warn');
    if (warn) {
      warn.textContent = Array.isArray(e.detail)
        ? 'Publish rejected: ' + e.detail.join(' ')
        : 'Publish rejected: ' + (e.message || e);
      warn.style.display = '';
    } else {
      _err('Publish rejected: ' + (e.message || e));
    }
  } finally {
    _refreshBudgetState();   // re-enables the button per current validity
  }
}

async function _rollbackBudget(archiveName) {
  const msg = (_budgetDirty && _budgetDirty.isDirty())
    ? `Roll back to ${archiveName}? This discards your unsaved edits. Rollback is itself a logged publish (the current caps are archived first).`
    : `Roll back to ${archiveName}? Rollback is itself a logged publish (the current caps are archived first).`;
  if (!window.confirm(msg)) return;
  try {
    await _post('/api/config/budget/rollback', { archive_name: archiveName });
    _toast('Rolled back to ' + archiveName);
    if (_budgetDirty) _budgetDirty.set(false);
    await _loadBudget(true);
  } catch (e) {
    if (_gate('budget', e)) return;
    _err('Rollback failed: ' + (Array.isArray(e.detail) ? e.detail.join(' ') : (e.message || e)));
  }
}

// --- Effective: read-only source-of-truth table ---------------------------------
const _SURFACE = {
  runtime: { word: 'runtime', kind: 'crew-st-ok' },
  needs_redeploy: { word: 'needs redeploy', kind: 'crew-st-block' },
  deploy_only: { word: 'deploy only', kind: 'crew-st-stop' },
};

async function _loadEffective() {
  let r;
  try {
    r = await _api('/api/config/effective');
  } catch (e) {
    if (_gate('effective', e)) return;
    _err('Could not load effective config: ' + (e.message || e));
    return;
  }
  _ungate('effective');
  const items = (r && r.items) || [];
  const tbody = _el('cfg-effective-rows');
  const empty = _el('cfg-effective-empty');
  const wrap = _el('cfg-effective-tablewrap');
  const banner = _el('cfg-effective-redeploy-banner');
  if (tbody) tbody.innerHTML = '';
  if (empty) empty.style.display = items.length ? 'none' : '';
  if (wrap) wrap.style.display = items.length ? '' : 'none';

  let anyRedeploy = false;
  for (const it of items) {
    if (it.surface === 'needs_redeploy') anyRedeploy = true;
    const tr = document.createElement('tr');
    tr.appendChild(_td(it.name, 'harness-mono'));
    let valText;
    if (it.value == null) valText = '—';
    else if (typeof it.value === 'object') valText = JSON.stringify(it.value);
    else valText = String(it.value);
    tr.appendChild(_td(valText, 'harness-mono'));
    tr.appendChild(_td(it.source));
    const s = _SURFACE[it.surface] || { word: it.surface || '—', kind: 'crew-st-stop' };
    tr.appendChild(_td(_badge(s.word, s.kind)));
    tr.appendChild(_td(it.editable_where));
    if (tbody) tbody.appendChild(tr);
  }
  if (banner) {
    if (anyRedeploy) {
      banner.textContent = 'Some settings only take effect after a redeploy — editing them in-app does not change the running process until it restarts.';
      banner.style.display = '';
    } else {
      banner.style.display = 'none';
    }
  }
}

// --- Providers: AI endpoints + API keys (surfaces /api/model-endpoints) ---------
// The provider CRUD + encrypted-at-rest key storage + test-connection already
// exist server-side (routes/model_routes.py, gate `require_admin` — the admin
// cookie passes). This tab surfaces them; it adds NO new backend. Security: the
// plaintext key is WRITE-ONLY here — the server never returns it (only has_key +
// a sha256[:8] fingerprint), so we render a masked fingerprint, offer
// replace-only rotation, and clear the key field after every send. No API key
// ever lives in this module's state.
const _PROVIDERS = [
  { label: 'Custom URL', url: '' },
  { label: 'OpenRouter', url: 'https://openrouter.ai/api/v1' },
  { label: 'OpenAI', url: 'https://api.openai.com/v1' },
  { label: 'Anthropic', url: 'https://api.anthropic.com' },
  { label: 'DeepSeek', url: 'https://api.deepseek.com/v1' },
  { label: 'Groq', url: 'https://api.groq.com/openai/v1' },
  { label: 'Mistral', url: 'https://api.mistral.ai/v1' },
  { label: 'Google Gemini', url: 'https://generativelanguage.googleapis.com/v1beta/openai' },
  { label: 'xAI Grok', url: 'https://api.x.ai/v1' },
  { label: 'Together AI', url: 'https://api.together.xyz/v1' },
  { label: 'Fireworks AI', url: 'https://api.fireworks.ai/inference/v1' },
  { label: 'Ollama Cloud', url: 'https://ollama.com/api' },
  { label: 'Z.AI (Zhipu)', url: 'https://api.z.ai/api/paas/v4' },
];

// The model-endpoint routes take FORM bodies for create/test and a JSON body
// for PATCH — separate helpers so the content-type is always right.
function _postForm(path, fields) {
  const body = new URLSearchParams();
  for (const [k, v] of Object.entries(fields)) body.set(k, v == null ? '' : String(v));
  return _api(path, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body });
}
function _patchJson(path, body) {
  return _api(path, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
}
function _del(path) { return _api(path, { method: 'DELETE' }); }

function _provMsg(text, bad) {
  const m = _el('cfg-prov-msg');
  if (!m) return;
  if (!text) { m.style.display = 'none'; m.textContent = ''; m.classList.remove('is-bad'); return; }
  m.textContent = text; m.style.display = ''; m.classList.toggle('is-bad', !!bad);
}

function _kv(k, v) {
  const s = document.createElement('span');
  s.className = 'cfg-prov-kv';
  if (k) {
    const kk = document.createElement('span');
    kk.className = 'cfg-prov-kv-k';
    kk.textContent = k;
    s.appendChild(kk);
  }
  s.appendChild(document.createTextNode(v == null ? '—' : String(v)));
  return s;
}

function _populateProviderSelect() {
  const sel = _el('cfg-prov-select');
  if (!sel || sel.options.length) return;   // populate once
  for (const p of _PROVIDERS) {
    const o = document.createElement('option');
    o.value = p.url;
    o.textContent = p.label;
    sel.appendChild(o);
  }
  sel.addEventListener('change', () => {
    const url = _el('cfg-prov-url');
    if (url) url.value = sel.value;   // prefill the base URL from the picked provider
  });
}

function _statusKind(s) {
  return s === 'online' ? 'crew-st-ok' : (s === 'empty' ? 'crew-st-block' : 'crew-st-stop');
}

function _renderProviderList(rows) {
  const box = _el('cfg-prov-list');
  if (!box) return;
  box.innerHTML = '';
  if (!rows.length) {
    const e = document.createElement('div');
    e.className = 'crew-empty';
    e.textContent = 'No providers yet — add one above.';
    box.appendChild(e);
    return;
  }
  for (const ep of rows) {
    const card = document.createElement('div');
    card.className = 'cfg-prov-row';

    const head = document.createElement('div');
    head.className = 'cfg-prov-head';
    const name = document.createElement('span');
    name.className = 'cfg-prov-name';
    name.textContent = ep.name || ep.base_url || ep.id;
    head.append(name, _badge(ep.status || (ep.online ? 'online' : 'offline'), _statusKind(ep.status)));
    if (!ep.is_enabled) head.appendChild(_tag('disabled'));
    if (ep.endpoint_kind && ep.endpoint_kind !== 'auto') head.appendChild(_tag(ep.endpoint_kind));
    if (ep.model_type && ep.model_type !== 'llm') head.appendChild(_tag(ep.model_type));

    const meta = document.createElement('div');
    meta.className = 'cfg-prov-meta';
    meta.appendChild(_kv('url', ep.base_url));
    meta.appendChild(_kv('models', String((ep.models || []).length + (ep.hidden_count || 0))));
    // Key shown ONLY as a masked fingerprint — the server never returns the key.
    meta.appendChild(_kv('', ep.has_key ? ('key ••••' + (ep.api_key_fingerprint || '')) : 'no key'));
    if (ep.ping_error) meta.appendChild(_kv('error', ep.ping_error));

    const actions = document.createElement('div');
    actions.className = 'cfg-prov-actions';
    const tog = document.createElement('button');
    tog.type = 'button';
    tog.className = 'preview-env-btn';
    tog.textContent = ep.is_enabled ? 'Disable' : 'Enable';
    tog.addEventListener('click', () => _toggleProvider(ep));
    const rotWrap = document.createElement('span');
    rotWrap.className = 'cfg-prov-rotate';
    const rotInp = document.createElement('input');
    rotInp.type = 'password';
    rotInp.autocomplete = 'off';
    rotInp.className = 'preview-env-keyinput';
    rotInp.placeholder = 'replace key';
    const rotBtn = document.createElement('button');
    rotBtn.type = 'button';
    rotBtn.className = 'preview-env-btn';
    rotBtn.textContent = 'Rotate';
    rotBtn.addEventListener('click', () => _rotateKey(ep, rotInp));
    rotWrap.append(rotInp, rotBtn);
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'preview-env-btn';
    del.textContent = 'Delete';
    del.addEventListener('click', () => _deleteProvider(ep));
    actions.append(tog, rotWrap, del);

    card.append(head, meta, actions);
    box.appendChild(card);
  }
}

async function _loadProviders() {
  _populateProviderSelect();
  let rows;
  try {
    rows = await _api('/api/model-endpoints');
  } catch (e) {
    if (_gate('providers', e)) return;
    _err('Could not load providers: ' + (e.message || e));
    return;
  }
  _ungate('providers');
  _renderProviderList(rows || []);
}

async function _testProvider() {
  const url = (_el('cfg-prov-url')?.value || '').trim();
  const key = _el('cfg-prov-key')?.value || '';
  const kind = _el('cfg-prov-kind')?.value || 'auto';
  if (!url) { _provMsg('Enter a base URL to test.', true); return; }
  _provMsg('Testing…', false);
  const btn = _el('cfg-prov-test');
  if (btn) btn.disabled = true;
  try {
    const r = await _postForm('/api/model-endpoints/test', { base_url: url, api_key: key, endpoint_kind: kind });
    if (r.online) {
      const eg = r.count ? ` (e.g. ${(r.models || []).slice(0, 3).join(', ')})` : '';
      _provMsg(`Online — ${r.count} model${r.count === 1 ? '' : 's'} found${eg}.`, false);
    } else {
      _provMsg('Offline — ' + (r.ping_error || 'no response from the endpoint.'), true);
    }
  } catch (e) {
    if (_gate('providers', e)) return;
    _provMsg('Test failed: ' + (e.message || e), true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function _addProvider() {
  const sel = _el('cfg-prov-select');
  const url = (_el('cfg-prov-url')?.value || '').trim();
  const key = _el('cfg-prov-key')?.value || '';
  const type = _el('cfg-prov-type')?.value || 'llm';
  const kind = _el('cfg-prov-kind')?.value || 'auto';
  if (!url) { _provMsg('A base URL is required.', true); return; }
  const label = sel && sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex].textContent : '';
  const name = (label && label !== 'Custom URL') ? label : '';
  const btn = _el('cfg-prov-add');
  if (btn) btn.disabled = true;
  _provMsg('Adding…', false);
  try {
    await _postForm('/api/model-endpoints', { base_url: url, api_key: key, name, model_type: type, endpoint_kind: kind });
    _provMsg('', false);
    _toast('Provider added');
    if (_el('cfg-prov-key')) _el('cfg-prov-key').value = '';   // never leave a key in the field
    await _loadProviders();
  } catch (e) {
    if (_gate('providers', e)) return;
    _provMsg('Add failed: ' + (e.message || e), true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function _rotateKey(ep, inp) {
  const key = (inp?.value || '').trim();
  if (!key) { _provMsg('Enter a replacement key first.', true); return; }
  if (!window.confirm(`Replace the API key for ${ep.name || ep.base_url}? The old key is overwritten.`)) return;
  try {
    await _patchJson('/api/model-endpoints/' + encodeURIComponent(ep.id), { api_key: key });
    if (inp) inp.value = '';
    _toast('Key replaced');
    await _loadProviders();
  } catch (e) {
    if (_gate('providers', e)) return;
    _provMsg('Rotate failed: ' + (e.message || e), true);
  }
}

async function _toggleProvider(ep) {
  try {
    await _patchJson('/api/model-endpoints/' + encodeURIComponent(ep.id), { is_enabled: !ep.is_enabled });
    await _loadProviders();
  } catch (e) {
    if (_gate('providers', e)) return;
    _provMsg('Toggle failed: ' + (e.message || e), true);
  }
}

async function _deleteProvider(ep) {
  let deps = [];
  try {
    const d = await _api('/api/model-endpoints/' + encodeURIComponent(ep.id) + '/dependents');
    deps = (d && d.dependents) || [];
  } catch { /* non-fatal — still allow delete with a generic confirm */ }
  const warn = deps.length
    ? `\n\nThis also clears ${deps.length} setting(s) that use it:\n- ${deps.slice(0, 8).join('\n- ')}`
    : '';
  if (!window.confirm(`Delete provider ${ep.name || ep.base_url}?${warn}`)) return;
  try {
    await _del('/api/model-endpoints/' + encodeURIComponent(ep.id));
    _toast('Provider deleted');
    await _loadProviders();
  } catch (e) {
    if (_gate('providers', e)) return;
    _provMsg('Delete failed: ' + (e.message || e), true);
  }
}

// --- tabs + overlay --------------------------------------------------------------
// _LOADERS-style map: adding Policy/App-settings later is just a new tab button
// + panel + one entry here.
const _LOADERS = {
  budget: _loadBudget,
  providers: _loadProviders,
  effective: _loadEffective,
};

// Confirm-on-discard gate shared by tab-switch / close / (guarded) reload.
// On a CONFIRMED discard it actually discards — resets the buffer to server
// truth and clears the dirty flag — so reopening (or switching back) shows the
// clean server values and Publish is never left live over a value the admin
// explicitly chose to drop. (Previously the prompt only gated the action and
// left _buffer/_budgetDirty untouched, so a confirmed "Discard" was a no-op and
// the reopen path — guarded by _loaded['budget'] — resurrected the edits.)
function _confirmLeaveBudget(actionMsg) {
  if (_tab === 'budget' && _budgetDirty && _budgetDirty.isDirty()) {
    if (!_budgetDirty.confirmDiscard(actionMsg)) return false;
    _revertBudget();            // buffer <- server caps, re-render, recompute state
    _budgetDirty.set(false);
  }
  return true;
}

function _showTab(tab) {
  if (tab === _tab) { /* re-selecting current tab: nothing to guard */ }
  else if (!_confirmLeaveBudget('You have unsaved budget changes. Discard them and switch tabs?')) return;
  _tab = tab;
  document.querySelectorAll('#config-tabs .admin-tab').forEach((b) => {
    b.classList.toggle('active', b.dataset.cfgtab === tab);
  });
  document.querySelectorAll('#config-overlay .harness-panel').forEach((p) => {
    p.style.display = p.dataset.cfgpanel === tab ? '' : 'none';
  });
  const load = _LOADERS[tab];
  if (load && !_loaded[tab]) { _loaded[tab] = true; load(); }
}

function _openOverlay() {
  const ov = _el('config-overlay');
  if (!ov) return;
  ov.style.display = '';
  _open = true;
  // Do NOT reset _loaded / the buffer on open — this is what guards an unsaved
  // buffer from being clobbered by a reopen. Lazy-load only tabs not yet loaded.
  _showTab(_tab);
}
function _closeOverlay() {
  if (!_confirmLeaveBudget('You have unsaved budget changes. Discard them and close?')) return;
  const ov = _el('config-overlay');
  if (ov) ov.style.display = 'none';
  _open = false;
}

function refresh() { /* host-wide tool; nothing per-session */ }

function init(apiBase) {
  API_BASE = apiBase || '';
  if (_wired) return;        // idempotent — safe if called more than once
  _wired = true;

  _budgetDirty = createDirtyState({ flagEl: _el('cfg-dirty-flag') });

  _el('tool-config-btn')?.addEventListener('click', _openOverlay);
  _el('config-close')?.addEventListener('click', _closeOverlay);
  _el('config-tabs')?.addEventListener('click', (e) => {
    const btn = e.target.closest('.admin-tab[data-cfgtab]');
    if (btn) _showTab(btn.dataset.cfgtab);
  });
  document.addEventListener('keydown', (e) => {
    const ov = _el('config-overlay');
    if (e.key === 'Escape' && ov && ov.style.display !== 'none') _closeOverlay();
  });

  // Budget
  _el('cfg-budget-publish')?.addEventListener('click', _publishBudget);
  _el('cfg-budget-revert')?.addEventListener('click', _revertBudget);
  _el('cfg-budget-reload')?.addEventListener('click', () => _loadBudget(false));
  _el('cfg-budget-versions')?.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-archive]');
    if (btn) _rollbackBudget(btn.dataset.archive);
  });

  // Providers
  _el('cfg-prov-reload')?.addEventListener('click', _loadProviders);
  _el('cfg-prov-test')?.addEventListener('click', _testProvider);
  _el('cfg-prov-add')?.addEventListener('click', _addProvider);

  // Effective
  _el('cfg-effective-reload')?.addEventListener('click', _loadEffective);

  // Native page-unload guard (covers browser reload / tab close while dirty).
  window.addEventListener('beforeunload', (e) => {
    if (_budgetDirty && _budgetDirty.isDirty()) { e.preventDefault(); e.returnValue = ''; return ''; }
  });
}

const configPanelModule = { init, refresh, createDirtyState };
export default configPanelModule;
window.configPanelModule = configPanelModule;

// Self-initialise: module scripts are deferred, so the DOM is parsed by the time
// this runs. app.js is not owned by this slice, so we wire ourselves.
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => init(''));
} else {
  init('');
}
