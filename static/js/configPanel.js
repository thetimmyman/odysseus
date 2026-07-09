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
    if (it.danger) tr.classList.add('cfg-eff-danger');
    tr.appendChild(_td(it.name, 'harness-mono'));
    let valText;
    if (it.value == null) valText = '—';
    else if (typeof it.value === 'object') valText = JSON.stringify(it.value);
    else valText = String(it.value);
    tr.appendChild(_td(valText, 'harness-mono'));
    tr.appendChild(_td(it.source));
    const s = _SURFACE[it.surface] || { word: it.surface || '—', kind: 'crew-st-stop' };
    tr.appendChild(_td(_badge(s.word, s.kind)));
    // Danger-zone / server-owned rows are not editable from the structured tabs.
    const where = document.createElement('td');
    where.textContent = it.editable_where || '';
    if (it.danger || it.editable === false) {
      const lock = document.createElement('span');
      lock.className = 'harness-tag';
      lock.textContent = it.danger ? 'read-only · danger' : 'read-only';
      lock.style.marginRight = '6px';
      where.insertBefore(lock, where.firstChild);
    }
    tr.appendChild(where);
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

// --- Policy: structured safe-knob editor (over /api/harness/policy) --------------
// The SAFE policy fields get friendly typed inputs with client-side validation
// mirroring the server (src/routing_policy._validate_policy). Danger-zone knobs
// (sandbox image/allowlist, sensitivity ceiling, coordinator provider/endpoint,
// ABSIS) are shown READ-ONLY — they need security_admin via the raw Routing
// Harness > Policy tab. On publish we send the FULL current policy with only the
// safe fields overridden, so the danger-zone values are unchanged → the publish
// route sees no danger-zone change and the admin cookie suffices.
let _polServer = null;   // last GET /api/harness/policy (server truth, typed)
let _polBuf = null;      // working deep-clone; safe edits coerce into it
let _policyDirty = null;

const _VMODES = ['regression_guard', 'bug_fix', 'feature_addition', 'refactor_equivalence', 'security_fix', 'analysis_only'];
const _BENCH_GATES = ['schema_validity', 'policy_gate_compliance', 'domain_classification', 'approval_gate', 'arbitration', 'uncertainty_handling', 'failure_retry', 'consistency'];
const _POLICY_FIELDS = [
  { g: 'Verification', p: 'verification.defaultMode', l: 'Default mode', t: 'enum', opts: _VMODES },
  { g: 'Verification', p: 'verification.overconfidenceThreshold', l: 'Overconfidence threshold', t: 'num', min: 0, max: 1 },
  { g: 'Verification', p: 'verification.equivalenceStdoutComparison', l: 'Byte-wise stdout equivalence', t: 'bool' },
  { g: 'Coordinator', p: 'coordinator.temperature', l: 'Temperature', t: 'num', min: 0, max: 2 },
  { g: 'Coordinator', p: 'coordinator.maxTokens', l: 'Max tokens', t: 'int', min: 1 },
  { g: 'Coordinator', p: 'coordinator.benchmark.defaultReplays', l: 'Benchmark replays', t: 'int', min: 1, max: 100 },
  ..._BENCH_GATES.map((k) => ({ g: 'Benchmark gate thresholds', p: 'coordinator.benchmark.thresholds.' + k, l: k, t: 'num', min: 0, max: 1 })),
  { g: 'Limits', p: 'maxUntrustedTokens', l: 'Max untrusted tokens', t: 'int', min: 0, max: 8192 },
  { g: 'Limits', p: 'rawOutputMaxBytes', l: 'Raw output max bytes', t: 'int', min: 1 },
  { g: 'Sandbox resources', p: 'sandbox.cpus', l: 'CPUs', t: 'num', min: 0, max: 32, gt0: true },
  { g: 'Sandbox resources', p: 'sandbox.memoryGb', l: 'Memory (GB)', t: 'num', min: 0, max: 128, gt0: true },
  { g: 'Sandbox resources', p: 'sandbox.pidsLimit', l: 'PID limit', t: 'int', min: 1, max: 65536 },
  { g: 'Sandbox resources', p: 'sandbox.wallClockSeconds', l: 'Wall-clock (s)', t: 'int', min: 1, max: 3600 },
  { g: 'Sandbox resources', p: 'sandbox.maxOutputBytes', l: 'Max output bytes', t: 'int', min: 1, max: 104857600 },
  { g: 'Sandbox resources', p: 'sandbox.mountLabel', l: 'Mount label', t: 'enum', opts: ['z', 'Z', ''] },
  { g: 'Sandbox resources', p: 'sandbox.runAsHostUser', l: 'Run as host user', t: 'bool' },
];
const _POLICY_DANGER = [
  { p: 'remoteSensitivityCeiling', l: 'Remote sensitivity ceiling' },
  { p: 'coordinator.provider', l: 'Coordinator provider' },
  { p: 'coordinator.endpointName', l: 'Coordinator endpoint' },
  { p: 'sandbox.image', l: 'Sandbox image' },
  { p: 'sandbox.allowedCommands', l: 'Allowed commands', list: true },
  { p: 'absis.enabled', l: 'ABSIS enabled' },
  { p: 'absis.sshTarget', l: 'ABSIS ssh target' },
  { p: 'absis.kubectlExecPrefix', l: 'ABSIS kubectl prefix' },
];

function _get(o, p) { return p.split('.').reduce((a, k) => (a == null ? a : a[k]), o); }
function _set(o, p, v) {
  const ks = p.split('.'); const last = ks.pop(); let cur = o;
  for (const k of ks) { if (cur[k] == null || typeof cur[k] !== 'object') cur[k] = {}; cur = cur[k]; }
  cur[last] = v;
}

function _validatePolicy() {
  const reasons = [], bad = new Set();
  if (!_polBuf) return { reasons, bad };
  for (const f of _POLICY_FIELDS) {
    const v = _get(_polBuf, f.p);
    if (f.t === 'bool') continue;
    if (f.t === 'enum') {
      if (!f.opts.includes(v == null ? '' : String(v))) { reasons.push(`${f.l} is invalid`); bad.add(f.p); }
      continue;
    }
    if (typeof v !== 'number' || !isFinite(v)) { reasons.push(`${f.l} must be a number`); bad.add(f.p); continue; }
    if (f.t === 'int' && !Number.isInteger(v)) { reasons.push(`${f.l} must be a whole number`); bad.add(f.p); continue; }
    if (f.gt0 && v <= 0) { reasons.push(`${f.l} must be greater than 0`); bad.add(f.p); continue; }
    if (f.min != null && v < f.min) { reasons.push(`${f.l} must be ≥ ${f.min}`); bad.add(f.p); continue; }
    if (f.max != null && v > f.max) { reasons.push(`${f.l} must be ≤ ${f.max}`); bad.add(f.p); }
  }
  return { reasons, bad };
}

function _policyDirtyNow() { return _polServer && _polBuf && JSON.stringify(_polBuf) !== JSON.stringify(_polServer); }

function _refreshPolicyState() {
  const { reasons, bad } = _validatePolicy();
  for (const f of _POLICY_FIELDS) {
    const card = document.querySelector(`#cfg-policy-fields .cfg-pol-field[data-pp="${f.p}"]`);
    if (card) card.classList.toggle('is-bad', bad.has(f.p));
  }
  const warn = _el('cfg-policy-warn');
  if (warn) {
    if (reasons.length) { warn.textContent = reasons.join(' '); warn.style.display = ''; }
    else { warn.textContent = ''; warn.style.display = 'none'; }
  }
  const changed = _policyDirtyNow();
  if (_policyDirty) _policyDirty.set(!!changed);
  const pub = _el('cfg-policy-publish');
  if (pub) pub.disabled = !!reasons.length || !changed;
  const rev = _el('cfg-policy-revert');
  if (rev) rev.disabled = !changed;
}

function _polFieldInput(f) {
  const cur = _get(_polBuf, f.p);
  if (f.t === 'bool') {
    const inp = document.createElement('input');
    inp.type = 'checkbox'; inp.checked = !!cur; inp.className = 'cfg-pol-check';
    inp.addEventListener('change', () => { _set(_polBuf, f.p, inp.checked); _refreshPolicyState(); });
    return inp;
  }
  if (f.t === 'enum') {
    const sel = document.createElement('select');
    sel.className = 'cfg-prov-input cfg-pol-input';
    for (const o of f.opts) {
      const op = document.createElement('option');
      op.value = o; op.textContent = o === '' ? '(none)' : o;
      if (String(cur == null ? '' : cur) === o) op.selected = true;
      sel.appendChild(op);
    }
    sel.addEventListener('change', () => { _set(_polBuf, f.p, sel.value); _refreshPolicyState(); });
    return sel;
  }
  const inp = document.createElement('input');
  inp.type = 'number'; inp.className = 'preview-env-keyinput cfg-pol-input';
  inp.autocomplete = 'off'; inp.spellcheck = false;
  if (f.t === 'int') inp.step = '1'; else inp.step = 'any';
  if (f.min != null) inp.min = String(f.min);
  if (f.max != null) inp.max = String(f.max);
  inp.value = cur == null ? '' : String(cur);
  inp.addEventListener('input', () => {
    const raw = inp.value.trim();
    _set(_polBuf, f.p, raw === '' ? NaN : Number(raw));
    _refreshPolicyState();
  });
  return inp;
}

function _renderPolicy() {
  const box = _el('cfg-policy-fields');
  if (!box) return;
  box.innerHTML = '';
  let curGroup = null, grid = null;
  for (const f of _POLICY_FIELDS) {
    if (f.g !== curGroup) {
      curGroup = f.g;
      const h = document.createElement('div');
      h.className = 'harness-detail-h';
      h.textContent = f.g;
      box.appendChild(h);
      grid = document.createElement('div');
      grid.className = 'cfg-pol-grid';
      box.appendChild(grid);
    }
    const card = document.createElement('div');
    card.className = 'cfg-pol-field';
    card.dataset.pp = f.p;
    const lab = document.createElement('label');
    lab.className = 'cfg-pol-label';
    lab.textContent = f.l;
    const wrap = document.createElement('div');
    wrap.className = 'cfg-pol-inputwrap';
    wrap.appendChild(_polFieldInput(f));
    card.append(lab, wrap);
    grid.appendChild(card);
  }
}

function _renderPolicyDanger() {
  const box = _el('cfg-policy-danger');
  if (!box) return;
  box.innerHTML = '';
  const grid = document.createElement('div');
  grid.className = 'cfg-pol-grid';
  for (const d of _POLICY_DANGER) {
    const v = _get(_polServer || {}, d.p);
    const card = document.createElement('div');
    card.className = 'cfg-pol-field is-danger';
    const lab = document.createElement('div');
    lab.className = 'cfg-pol-label';
    lab.textContent = d.l;
    const val = document.createElement('div');
    val.className = 'cfg-pol-danger-val';
    val.textContent = d.list ? `${Array.isArray(v) ? v.length : 0} commands` : (v == null || v === '' ? '—' : String(v));
    card.append(lab, val);
    grid.appendChild(card);
  }
  box.appendChild(grid);
}

function _renderPolicyChips() {
  const chips = _el('cfg-policy-chips');
  if (!chips) return;
  chips.innerHTML = '';
  const v = _get(_polServer || {}, 'routingPolicyVersion');
  chips.appendChild(_tag(`policy ${v == null ? '—' : v}`, true));
}

function _renderPolicyVersions(versions) {
  const box = _el('cfg-policy-versions');
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
    name.textContent = v.archive;
    const meta = document.createElement('span');
    meta.className = 'harness-ver-meta';
    meta.textContent = `v${v.routingPolicyVersion} · ${_fmtTs(v.modified_at)}`;
    const rb = document.createElement('button');
    rb.type = 'button';
    rb.className = 'preview-env-btn';
    rb.textContent = 'Rollback';
    rb.dataset.archive = v.archive;
    row.append(name, meta, rb);
    box.appendChild(row);
  }
}

async function _loadPolicy(force) {
  if (!force && _policyDirty && _policyDirty.isDirty()) {
    if (!_policyDirty.confirmDiscard('Reload will discard your unsaved policy changes. Continue?')) return;
  }
  let cur, vers;
  try {
    [cur, vers] = await Promise.all([
      _api('/api/harness/policy'),
      _api('/api/harness/policy/versions'),
    ]);
  } catch (e) {
    if (_gate('policy', e)) return;
    _err('Could not load policy: ' + (e.message || e));
    return;
  }
  _ungate('policy');
  _polServer = (cur && cur.policy) || {};
  _polBuf = JSON.parse(JSON.stringify(_polServer));
  _renderPolicy();
  _renderPolicyDanger();
  _renderPolicyChips();
  _renderPolicyVersions((vers && vers.versions) || []);
  if (_policyDirty) _policyDirty.set(false);
  _refreshPolicyState();
}

function _revertPolicy() {
  if (!_polServer) return;
  _polBuf = JSON.parse(JSON.stringify(_polServer));
  _renderPolicy();
  _refreshPolicyState();
}

async function _publishPolicy() {
  const { reasons } = _validatePolicy();
  if (reasons.length) { _refreshPolicyState(); return; }
  if (!window.confirm(
    'Publish these policy changes?\n\nOnly the safe knobs shown here change; danger-zone '
    + 'values are preserved. The outgoing version is archived and the change is logged.')) return;
  const btn = _el('cfg-policy-publish');
  if (btn) btn.disabled = true;
  try {
    await _post('/api/harness/policy/publish', { policy: _polBuf });
    _toast('Policy published');
    if (_policyDirty) _policyDirty.set(false);
    await _loadPolicy(true);
  } catch (e) {
    if (_gate('policy', e)) return;
    const warn = _el('cfg-policy-warn');
    if (warn) {
      warn.textContent = Array.isArray(e.detail) ? 'Publish rejected: ' + e.detail.join(' ')
        : 'Publish rejected: ' + (e.message || e);
      warn.style.display = '';
    } else { _err('Publish rejected: ' + (e.message || e)); }
  } finally {
    _refreshPolicyState();
  }
}

async function _rollbackPolicy(archive) {
  const msg = (_policyDirty && _policyDirty.isDirty())
    ? `Roll back to ${archive}? This discards your unsaved edits. Rollback is itself a logged publish.`
    : `Roll back to ${archive}? Rollback is itself a logged publish (the current policy is archived first).`;
  if (!window.confirm(msg)) return;
  try {
    await _post('/api/harness/policy/rollback', { archive });
    _toast('Rolled back to ' + archive);
    if (_policyDirty) _policyDirty.set(false);
    await _loadPolicy(true);
  } catch (e) {
    if (_gate('policy', e)) return;
    // A rollback that re-instates a danger-zone value needs security_admin → 403.
    _err('Rollback failed: ' + (e.status === 403
      ? 'that archived policy changes a danger-zone value — it needs a security_admin (Routing Harness › Policy).'
      : (Array.isArray(e.detail) ? e.detail.join(' ') : (e.message || e))));
  }
}

// --- tabs + overlay --------------------------------------------------------------
// _LOADERS-style map: adding another tab later is just a new tab button + panel
// + one entry here.
const _LOADERS = {
  budget: _loadBudget,
  providers: _loadProviders,
  policy: _loadPolicy,
  effective: _loadEffective,
};

// Confirm-on-discard gate shared by tab-switch / close / (guarded) reload.
// On a CONFIRMED discard it actually discards — resets the buffer to server
// truth and clears the dirty flag — so reopening (or switching back) shows the
// clean server values and Publish is never left live over a value the admin
// explicitly chose to drop. (Previously the prompt only gated the action and
// left _buffer/_budgetDirty untouched, so a confirmed "Discard" was a no-op and
// the reopen path — guarded by _loaded['budget'] — resurrected the edits.)
function _confirmLeave(actionMsg) {
  if (_tab === 'budget' && _budgetDirty && _budgetDirty.isDirty()) {
    if (!_budgetDirty.confirmDiscard(actionMsg)) return false;
    _revertBudget();            // buffer <- server caps, re-render, recompute state
    _budgetDirty.set(false);
  }
  if (_tab === 'policy' && _policyDirty && _policyDirty.isDirty()) {
    if (!_policyDirty.confirmDiscard(actionMsg)) return false;
    _revertPolicy();
    _policyDirty.set(false);
  }
  return true;
}

function _showTab(tab) {
  if (tab === _tab) { /* re-selecting current tab: nothing to guard */ }
  else if (!_confirmLeave('You have unsaved changes. Discard them and switch tabs?')) return;
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
  if (!_confirmLeave('You have unsaved changes. Discard them and close?')) return;
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
  // Shares the header flag with budget — safe because switching tabs reverts the
  // leaving tab, so only the active tab is ever dirty at once.
  _policyDirty = createDirtyState({ flagEl: _el('cfg-dirty-flag') });

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

  // Policy
  _el('cfg-policy-publish')?.addEventListener('click', _publishPolicy);
  _el('cfg-policy-revert')?.addEventListener('click', _revertPolicy);
  _el('cfg-policy-reload')?.addEventListener('click', () => _loadPolicy(false));
  _el('cfg-policy-versions')?.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-archive]');
    if (btn) _rollbackPolicy(btn.dataset.archive);
  });

  // Effective
  _el('cfg-effective-reload')?.addEventListener('click', _loadEffective);

  // Native page-unload guard (covers browser reload / tab close while dirty).
  window.addEventListener('beforeunload', (e) => {
    const dirty = (_budgetDirty && _budgetDirty.isDirty()) || (_policyDirty && _policyDirty.isDirty());
    if (dirty) { e.preventDefault(); e.returnValue = ''; return ''; }
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
