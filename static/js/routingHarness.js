// static/js/routingHarness.js — Routing Harness admin tool: coordinator
// decision audit viewer + manual wrap workflow, model-profile registry,
// versioned routing policy, budget dashboard, route preview, and break-glass
// emergency overrides. UI slice over routes/routing_harness_routes.py.
//
// Mirrors devPreview/crewPanel: a Tools overlay, cookie _api (same-origin,
// credentials carried), XSS-safe rendering (textContent / _esc only), and
// display-side admin gating — every /api/harness route enforces the admin
// cookie server-side; on 401/403 each panel shows ONE inline "Admin session
// required" state instead of crashing. The Emergency tab additionally
// surfaces the security_admin refusal (normal admin cookies are rejected
// for break-glass by design).

let API_BASE = '';
let _open = false;
let _tab = 'decisions';
let _loaded = {};            // tab -> has loaded at least once this open
let _auditSel = null;        // selected audit row id
let _registry = [];          // last GET /registry payload
let _regEditing = null;      // profile id with the inline editor open
let _emRows = [];            // last GET /emergency/active payload
let _emTimer = null;         // 1s countdown interval (Emergency tab only)

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
// Same-origin PATCH/DELETE: the browser auto-attaches Origin + Sec-Fetch-Site,
// satisfying the server's CSRF guard; credentials:'same-origin' carries the
// admin cookie (same pattern as devPreview's _put/_del).
function _patch(path, body) {
  return _api(path, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
}
function _del(path) {
  return _api(path, { method: 'DELETE' });
}

// --- shared render helpers -----------------------------------------------------
function _isAuthErr(e) { return e && (e.status === 401 || e.status === 403); }

// One inline "Admin session required" state per panel; never crash the tab.
function _gate(tab, e) {
  if (!_isAuthErr(e)) return false;
  const panel = document.querySelector(`#harness-overlay .harness-panel[data-rhpanel="${tab}"]`);
  if (!panel) return true;
  const gate = panel.querySelector('.harness-gate');
  const content = panel.querySelector('.harness-panel-content');
  if (gate) gate.style.display = '';
  if (content) content.style.display = 'none';
  return true;
}
function _ungate(tab) {
  const panel = document.querySelector(`#harness-overlay .harness-panel[data-rhpanel="${tab}"]`);
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
function _kv(key, value, mono) {
  const row = document.createElement('div');
  row.className = 'harness-kv';
  const k = document.createElement('span');
  k.className = 'harness-kv-k';
  k.textContent = key;
  const v = document.createElement('span');
  v.className = 'harness-kv-v' + (mono ? ' harness-mono' : '');
  if (value instanceof Node) v.appendChild(value);
  else v.textContent = value == null || value === '' ? '—' : String(value);
  row.append(k, v);
  return row;
}
function _sectionH(text) {
  const h = document.createElement('div');
  h.className = 'harness-detail-h';
  h.textContent = text;
  return h;
}
function _ul(items) {
  const ul = document.createElement('ul');
  ul.className = 'harness-list';
  for (const it of items || []) {
    const li = document.createElement('li');
    li.textContent = typeof it === 'string' ? it : JSON.stringify(it);
    ul.appendChild(li);
  }
  return ul;
}
function _pre(text) {
  const pre = document.createElement('pre');
  pre.className = 'harness-pre';
  pre.textContent = text == null ? '' : String(text);
  return pre;
}
// Policy-version stamps ({routingPolicyVersion: "1.0", ...}) as small chips.
function _versionChips(pv) {
  const wrap = document.createElement('span');
  if (!pv || typeof pv !== 'object') return wrap;
  for (const [k, v] of Object.entries(pv)) {
    wrap.appendChild(_tag(`${k.replace(/PolicyVersion$|Version$/, '')} ${v}`, true));
  }
  return wrap;
}
function _parseJsonInput(raw, what) {
  try {
    const v = JSON.parse(raw);
    if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error('must be a JSON object');
    return v;
  } catch (e) {
    _err(`${what}: invalid JSON (${e.message || e})`);
    return null;
  }
}

// --- Decisions: coordinator audit archive + manual wrap ------------------------
async function _loadAudit() {
  const tbody = _el('harness-audit-rows');
  if (!tbody) return;
  const filter = (_el('harness-audit-filter').value || '').trim();
  let rows;
  try {
    const q = filter ? `&task_id=${encodeURIComponent(filter)}` : '';
    rows = await _api(`/api/harness/coordinator/audit?limit=100${q}`);
  } catch (e) {
    if (_gate('decisions', e)) return;
    _err('Could not load audit rows: ' + (e.message || e));
    return;
  }
  _ungate('decisions');
  tbody.innerHTML = '';
  _el('harness-audit-empty').style.display = rows.length ? 'none' : '';
  _el('harness-audit-tablewrap').style.display = rows.length ? '' : 'none';
  for (const r of rows) {
    const tr = document.createElement('tr');
    tr.className = 'is-clickable';
    if (r.id === _auditSel) tr.classList.add('selected');
    tr.dataset.auditId = r.id;
    tr.appendChild(_td(_fmtTs(r.created_at), 'harness-nowrap'));
    tr.appendChild(_td(r.task_id || '—', 'harness-mono'));
    tr.appendChild(_td(_badge(r.parsed_ok ? 'parsed' : 'failed', r.parsed_ok ? 'crew-st-ok' : 'crew-st-err')));
    tr.appendChild(_td(r.fallback_path || (r.applied_fallback ? 'fallback' : '—'), 'harness-mono'));
    tr.appendChild(_td(r.schema_version, 'harness-nowrap'));
    tbody.appendChild(tr);
  }
  // keep the detail pane in sync if the selected row disappeared
  if (_auditSel && !rows.some((r) => r.id === _auditSel)) {
    _auditSel = null;
    _el('harness-audit-detail').style.display = 'none';
  }
}

async function _selectAudit(id) {
  _auditSel = id;
  document.querySelectorAll('#harness-audit-rows tr').forEach((tr) => {
    tr.classList.toggle('selected', tr.dataset.auditId === id);
  });
  const pane = _el('harness-audit-detail');
  if (!pane) return;
  let d;
  try {
    d = await _api(`/api/harness/coordinator/audit/${encodeURIComponent(id)}`);
  } catch (e) {
    if (_gate('decisions', e)) return;
    _err('Could not load audit detail: ' + (e.message || e));
    return;
  }
  pane.innerHTML = '';
  pane.style.display = '';

  const head = document.createElement('div');
  head.className = 'harness-row';
  head.appendChild(_badge(d.parsed_ok ? 'parsed ok' : 'parse failed', d.parsed_ok ? 'crew-st-ok' : 'crew-st-err'));
  if (d.applied_fallback) head.appendChild(_badge('fallback applied', 'crew-st-block'));
  if (d.redaction_applied) head.appendChild(_badge('redacted', 'crew-st-block'));
  pane.appendChild(head);

  pane.appendChild(_kv('audit id', d.id, true));
  pane.appendChild(_kv('task id', d.task_id, true));
  pane.appendChild(_kv('created', _fmtTs(d.created_at)));
  pane.appendChild(_kv('schema version', d.schema_version, true));
  pane.appendChild(_kv('fallback path', d.fallback_path, true));
  if (d.hmac) {
    const h = document.createElement('span');
    h.className = 'harness-mono';
    h.textContent = String(d.hmac).slice(0, 20) + (String(d.hmac).length > 20 ? '…' : '');
    h.title = d.hmac;
    pane.appendChild(_kv('hmac', h));
  }
  if (d.policy_versions) pane.appendChild(_kv('policy versions', _versionChips(d.policy_versions)));

  const errs = d.validation_errors || [];
  pane.appendChild(_sectionH(`Validation errors (${errs.length})`));
  pane.appendChild(errs.length ? _ul(errs) : Object.assign(document.createElement('div'), { className: 'harness-note', textContent: 'none' }));

  const notes = d.audit_notes || [];
  pane.appendChild(_sectionH(`Audit notes (${notes.length})`));
  pane.appendChild(notes.length ? _ul(notes) : Object.assign(document.createElement('div'), { className: 'harness-note', textContent: 'none' }));

  pane.appendChild(_sectionH('Raw output' + (d.redaction_applied ? ' (stored redacted)' : '')));
  pane.appendChild(_pre(d.raw_output || ''));
}

async function _wrapSubmit() {
  const taskId = (_el('harness-wrap-task').value || '').trim();
  const raw = _el('harness-wrap-raw').value || '';
  if (!taskId) { _err('task id is required'); return; }
  if (!raw.trim()) { _err('raw coordinator output is required'); return; }
  const btn = _el('harness-wrap-submit');
  btn.disabled = true;
  try {
    const r = await _post('/api/harness/coordinator/wrap', {
      task_id: taskId,
      raw_coordinator_output: raw,
      approval_satisfied: _el('harness-wrap-approval').checked,
      remote_exception_approved: _el('harness-wrap-remote').checked,
    });
    const box = _el('harness-wrap-result');
    box.innerHTML = '';
    box.style.display = '';
    const head = document.createElement('div');
    head.className = 'harness-row';
    head.appendChild(_badge(r.ok ? 'ok' : 'rejected', r.ok ? 'crew-st-ok' : 'crew-st-err'));
    if (r.appliedFallback) head.appendChild(_badge('fallback applied', 'crew-st-block'));
    box.appendChild(head);
    box.appendChild(_kv('fallback path', r.fallbackPath, true));
    box.appendChild(_kv('audit id', r.auditId, true));
    if ((r.validationErrors || []).length) {
      box.appendChild(_sectionH(`Validation errors (${r.validationErrors.length})`));
      box.appendChild(_ul(r.validationErrors));
    }
    if ((r.auditNotes || []).length) {
      box.appendChild(_sectionH(`Audit notes (${r.auditNotes.length})`));
      box.appendChild(_ul(r.auditNotes));
    }
    box.appendChild(_sectionH('Resulting route'));
    box.appendChild(_pre(r.route ? JSON.stringify(r.route, null, 2) : 'none — fail-closed (human_only)'));
    _toast('Decision wrapped & archived');
    await _loadAudit();
    if (r.auditId) _selectAudit(r.auditId);
  } catch (e) {
    if (_gate('decisions', e)) return;
    _err('Wrap failed: ' + (e.message || e));
  } finally {
    btn.disabled = false;
  }
}

// --- Registry: model profiles --------------------------------------------------
async function _loadRegistry() {
  const tbody = _el('harness-registry-rows');
  if (!tbody) return;
  try {
    _registry = await _api('/api/harness/registry');
  } catch (e) {
    if (_gate('registry', e)) return;
    _err('Could not load registry: ' + (e.message || e));
    return;
  }
  _ungate('registry');
  tbody.innerHTML = '';
  _el('harness-registry-empty').style.display = _registry.length ? 'none' : '';
  _el('harness-registry-tablewrap').style.display = _registry.length ? '' : 'none';
  for (const p of _registry) {
    const tr = document.createElement('tr');
    tr.dataset.profileId = p.id;
    tr.appendChild(_td(p.id, 'harness-mono'));
    tr.appendChild(_td(p.model, 'harness-mono'));
    tr.appendChild(_td(p.endpoint ? p.endpoint.name : '—'));
    const roles = document.createElement('span');
    (p.roles || []).forEach((r) => roles.appendChild(_tag(r)));
    if (!(p.roles || []).length) roles.textContent = '—';
    tr.appendChild(_td(roles));
    tr.appendChild(_td(p.context_window == null ? '—' : p.context_window.toLocaleString(), 'harness-nowrap'));
    tr.appendChild(_td(p.input_cost_per_mtok == null ? '—' : _fmtUsd(p.input_cost_per_mtok, 2), 'harness-nowrap'));
    tr.appendChild(_td(p.output_cost_per_mtok == null ? '—' : _fmtUsd(p.output_cost_per_mtok, 2), 'harness-nowrap'));
    const tier = document.createElement('span');
    if (p.is_free) tier.appendChild(_badge('free', 'crew-st-ok'));
    if (p.is_premium) tier.appendChild(_badge('premium', 'crew-st-block'));
    if (!p.is_free && !p.is_premium) tier.textContent = '—';
    tr.appendChild(_td(tier));
    const sw = document.createElement('input');
    sw.type = 'checkbox';
    sw.className = 'preview-cfg-toggle';
    sw.checked = !!p.enabled;
    sw.title = p.enabled ? 'Enabled — click to disable' : 'Disabled — click to enable';
    sw.dataset.act = 'toggle';
    sw.dataset.profileId = p.id;
    tr.appendChild(_td(sw));
    const acts = document.createElement('span');
    const edit = document.createElement('button');
    edit.type = 'button'; edit.className = 'preview-env-btn'; edit.textContent = 'Edit';
    edit.dataset.act = 'edit'; edit.dataset.profileId = p.id;
    const del = document.createElement('button');
    del.type = 'button'; del.className = 'preview-env-btn preview-env-btn-clear'; del.textContent = 'Delete';
    del.dataset.act = 'delete'; del.dataset.profileId = p.id;
    acts.append(edit, del);
    tr.appendChild(_td(acts, 'harness-nowrap'));
    tbody.appendChild(tr);

    if (_regEditing === p.id) tbody.appendChild(_regEditRow(p));
  }
}

function _regEditRow(p) {
  const tr = document.createElement('tr');
  const td = document.createElement('td');
  td.colSpan = 10;
  const form = document.createElement('div');
  form.className = 'harness-edit-form';

  const mk = (ph, val, cls) => {
    const i = document.createElement('input');
    i.type = 'text'; i.placeholder = ph; i.autocomplete = 'off'; i.spellcheck = false;
    i.className = 'preview-env-keyinput' + (cls ? ' ' + cls : '');
    i.value = val == null ? '' : String(val);
    return i;
  };
  const roles = mk('roles (comma-separated)', (p.roles || []).join(', '));
  const ctx = mk('context window', p.context_window, 'harness-num');
  const inc = mk('$/Mtok in', p.input_cost_per_mtok, 'harness-num');
  const outc = mk('$/Mtok out', p.output_cost_per_mtok, 'harness-num');
  const notes = mk('notes', p.notes, 'harness-wide');

  const row1 = document.createElement('div'); row1.className = 'harness-row';
  row1.append(roles, ctx, inc, outc);
  const row2 = document.createElement('div'); row2.className = 'harness-row';
  row2.appendChild(notes);
  const row3 = document.createElement('div'); row3.className = 'harness-row';
  const save = document.createElement('button');
  save.type = 'button'; save.className = 'preview-env-btn preview-env-btn-save'; save.textContent = 'Save';
  const cancel = document.createElement('button');
  cancel.type = 'button'; cancel.className = 'preview-env-btn'; cancel.textContent = 'Cancel';
  row3.append(save, cancel);
  form.append(row1, row2, row3);
  td.appendChild(form);
  tr.appendChild(td);

  const _num = (inp, integer) => {
    const s = inp.value.trim();
    if (s === '') return null;
    const n = integer ? parseInt(s, 10) : parseFloat(s);
    return isNaN(n) ? undefined : n;   // undefined = invalid, null = clear
  };
  save.addEventListener('click', async () => {
    const body = {
      roles: roles.value.split(',').map((s) => s.trim()).filter(Boolean),
      notes: notes.value.trim() || null,
    };
    const cw = _num(ctx, true), ic = _num(inc, false), oc = _num(outc, false);
    if (cw === undefined || ic === undefined || oc === undefined) { _err('Costs and context window must be numbers'); return; }
    if (cw !== null) body.context_window = cw;
    if (ic !== null) body.input_cost_per_mtok = ic;
    if (oc !== null) body.output_cost_per_mtok = oc;
    try {
      await _patch(`/api/harness/registry/${encodeURIComponent(p.id)}`, body);
      _regEditing = null;
      _toast('Saved ' + p.id);
      await _loadRegistry();
    } catch (e) {
      if (_gate('registry', e)) return;
      _err('Save failed: ' + (e.message || e));
    }
  });
  cancel.addEventListener('click', () => { _regEditing = null; _loadRegistry(); });
  return tr;
}

async function _regToggle(id, checkbox) {
  checkbox.disabled = true;
  try {
    await _patch(`/api/harness/registry/${encodeURIComponent(id)}`, { enabled: checkbox.checked });
    _toast((checkbox.checked ? 'Enabled ' : 'Disabled ') + id);
  } catch (e) {
    checkbox.checked = !checkbox.checked;   // revert to server truth
    if (_gate('registry', e)) return;
    _err('Toggle failed: ' + (e.message || e));
  } finally {
    checkbox.disabled = false;
  }
}

async function _regDelete(id) {
  if (!window.confirm(`Delete profile ${id}? Profiles with recorded runs are refused (disable instead).`)) return;
  try {
    await _del(`/api/harness/registry/${encodeURIComponent(id)}`);
    _toast('Deleted ' + id);
    await _loadRegistry();
  } catch (e) {
    if (_gate('registry', e)) return;
    // Surfaces the server's 400 refusal ("profile has recorded model runs —
    // disable instead of delete") verbatim.
    _err('Delete refused: ' + (e.message || e));
  }
}

async function _regCreate() {
  const val = (id) => (_el(id).value || '').trim();
  const id = val('harness-reg-id');
  const model = val('harness-reg-model');
  if (!id || !model) { _err('profile id and model are required'); return; }
  const body = {
    id, model,
    roles: val('harness-reg-roles').split(',').map((s) => s.trim()).filter(Boolean),
    is_free: _el('harness-reg-free').checked,
    is_premium: _el('harness-reg-premium').checked,
    notes: val('harness-reg-notes') || null,
  };
  const nums = [
    ['harness-reg-ctx', 'context_window', true],
    ['harness-reg-incost', 'input_cost_per_mtok', false],
    ['harness-reg-outcost', 'output_cost_per_mtok', false],
  ];
  for (const [elId, field, integer] of nums) {
    const s = val(elId);
    if (!s) continue;
    const n = integer ? parseInt(s, 10) : parseFloat(s);
    if (isNaN(n)) { _err(field + ' must be a number'); return; }
    body[field] = n;
  }
  try {
    await _post('/api/harness/registry', body);
    _toast('Created ' + id);
    ['harness-reg-id', 'harness-reg-model', 'harness-reg-roles', 'harness-reg-ctx',
      'harness-reg-incost', 'harness-reg-outcost', 'harness-reg-notes'].forEach((i) => { _el(i).value = ''; });
    _el('harness-reg-free').checked = false;
    _el('harness-reg-premium').checked = false;
    await _loadRegistry();
  } catch (e) {
    if (_gate('registry', e)) return;
    _err('Create failed: ' + (e.message || e));
  }
}

// --- Policy: versioned config --------------------------------------------------
async function _loadPolicy() {
  try {
    const [cur, vers] = await Promise.all([
      _api('/api/harness/policy'),
      _api('/api/harness/policy/versions'),
    ]);
    _ungate('policy');
    _el('harness-policy-json').value = JSON.stringify(cur.policy, null, 2);
    const chips = _el('harness-policy-chips');
    chips.innerHTML = '';
    chips.appendChild(_versionChips(cur.policyVersions));
    _renderPolicyVersions(vers.versions || []);
  } catch (e) {
    if (_gate('policy', e)) return;
    _err('Could not load policy: ' + (e.message || e));
  }
}

function _renderPolicyVersions(versions) {
  const box = _el('harness-policy-versions');
  box.innerHTML = '';
  if (!versions.length) {
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

async function _policyPublish() {
  const policy = _parseJsonInput(_el('harness-policy-json').value, 'Policy');
  if (!policy) return;
  if (!window.confirm('Publish this policy? The outgoing file is archived and the change is logged.')) return;
  const btn = _el('harness-policy-publish');
  btn.disabled = true;
  try {
    const r = await _post('/api/harness/policy/publish', { policy });
    _el('harness-policy-json').value = JSON.stringify(r.policy, null, 2);
    _toast('Policy published');
    await _loadPolicy();
  } catch (e) {
    if (_gate('policy', e)) return;
    _err('Publish rejected: ' + (e.message || e));
  } finally {
    btn.disabled = false;
  }
}

async function _policyRollback(archive) {
  if (!window.confirm(`Roll back to ${archive}? Rollback is itself a logged publish (the current policy is archived first).`)) return;
  try {
    await _post('/api/harness/policy/rollback', { archive });
    _toast('Rolled back to ' + archive);
    await _loadPolicy();
  } catch (e) {
    if (_gate('policy', e)) return;
    _err('Rollback failed: ' + (e.message || e));
  }
}

// --- Budget: caps + spend + per-task preview -----------------------------------
async function _loadBudget() {
  let s;
  try {
    s = await _api('/api/harness/budget/summary');
  } catch (e) {
    if (_gate('budget', e)) return;
    _err('Could not load budget summary: ' + (e.message || e));
    return;
  }
  _ungate('budget');
  const box = _el('harness-budget-stats');
  box.innerHTML = '';
  const periods = s.periods || {};
  for (const name of ['daily', 'weekly', 'monthly']) {
    const p = periods[name];
    if (!p) continue;
    const card = document.createElement('div');
    card.className = 'admin-card harness-stat';
    const over = p.cap_usd != null && p.spend_usd >= p.cap_usd;
    const pOver = p.premium_cap_usd != null && p.premium_spend_usd >= p.premium_cap_usd;
    if (over || pOver) card.classList.add('is-over');
    const k = document.createElement('div');
    k.className = 'harness-stat-k';
    k.textContent = name;
    const v = document.createElement('div');
    v.className = 'harness-stat-v';
    v.textContent = `${_fmtUsd(p.spend_usd)} / ${p.cap_usd == null ? 'no cap' : _fmtUsd(p.cap_usd)}`;
    const sub = document.createElement('div');
    sub.className = 'harness-stat-sub';
    sub.textContent = `premium ${_fmtUsd(p.premium_spend_usd)} / ${p.premium_cap_usd == null ? 'no cap' : _fmtUsd(p.premium_cap_usd)} · ${p.runs} run${p.runs === 1 ? '' : 's'}`;
    card.append(k, v, sub);
    box.appendChild(card);
  }
  const chips = _el('harness-budget-chips');
  chips.innerHTML = '';
  chips.appendChild(_versionChips(s.policyVersions));
}

async function _budgetPreview() {
  const task = _parseJsonInput(_el('harness-budget-task').value, 'Task');
  if (!task) return;
  const btn = _el('harness-budget-preview');
  btn.disabled = true;
  try {
    const r = await _post('/api/harness/budget/preview', { task });
    const box = _el('harness-budget-result');
    box.innerHTML = '';
    box.style.display = '';
    const head = document.createElement('div');
    head.className = 'harness-row';
    head.appendChild(_badge(r.allowed ? 'allowed' : 'blocked', r.allowed ? 'crew-st-ok' : 'crew-st-err'));
    box.appendChild(head);
    box.appendChild(_kv('general check', r.general && r.general.allowed ? 'ok' : (r.general && r.general.reason) || 'blocked'));
    box.appendChild(_kv('premium check', r.premium && r.premium.allowed ? 'ok' : (r.premium && r.premium.reason) || 'blocked'));
    if (r.premiumAllowance) {
      box.appendChild(_kv('task allows premium', r.premiumAllowance.taskAllowsPremium ? 'yes' : 'no'));
      box.appendChild(_kv('budget allows premium', r.premiumAllowance.budgetAllowsPremium ? 'yes' : 'no'));
    }
    box.appendChild(_kv('task cap', r.taskCapUsd == null ? 'none' : _fmtUsd(r.taskCapUsd)));
    box.appendChild(_sectionH('Current spend'));
    box.appendChild(_pre(JSON.stringify(r.spend || {}, null, 2)));
  } catch (e) {
    if (_gate('budget', e)) return;
    _err('Budget preview failed: ' + (e.message || e));
  } finally {
    btn.disabled = false;
  }
}

// --- Route preview: ranked candidates ------------------------------------------
async function _routePreview() {
  const task = _parseJsonInput(_el('harness-route-task').value, 'Task');
  if (!task) return;
  const btn = _el('harness-route-preview');
  btn.disabled = true;
  try {
    const r = await _post('/api/harness/route/preview', { task });
    _ungate('route');
    const tokens = _el('harness-route-tokens');
    tokens.style.display = '';
    tokens.textContent = `context estimate: ~${r.context_token_estimate} tokens`;
    const cands = r.candidates || [];
    _el('harness-route-tablewrap').style.display = cands.length ? '' : 'none';
    const empty = _el('harness-route-empty');
    empty.style.display = cands.length ? 'none' : '';
    empty.textContent = 'No candidates — no enabled registry profile passes this task\'s free/paid/premium flags.';
    const tbody = _el('harness-route-rows');
    tbody.innerHTML = '';
    cands.forEach((c, i) => {
      const tr = document.createElement('tr');
      tr.appendChild(_td(String(i + 1), 'harness-nowrap'));
      tr.appendChild(_td(c.profile_id, 'harness-mono'));
      tr.appendChild(_td(c.model, 'harness-mono'));
      const roles = document.createElement('span');
      (c.roles || []).forEach((role) => roles.appendChild(_tag(role)));
      if (!(c.roles || []).length) roles.textContent = '—';
      tr.appendChild(_td(roles));
      tr.appendChild(_td(String(c.score), 'harness-nowrap'));
      tr.appendChild(_td(_fmtUsd(c.estimated_cost_usd, 4), 'harness-nowrap'));
      const det = document.createElement('details');
      det.className = 'harness-reasons';
      const sum = document.createElement('summary');
      sum.textContent = `${(c.reasons || []).length} reason${(c.reasons || []).length === 1 ? '' : 's'}`;
      det.appendChild(sum);
      det.appendChild(_ul(c.reasons || []));
      tr.appendChild(_td(det));
      tbody.appendChild(tr);
    });
  } catch (e) {
    if (_gate('route', e)) return;
    _err('Route preview failed: ' + (e.message || e));
  } finally {
    btn.disabled = false;
  }
}

// --- Emergency: break-glass overrides -------------------------------------------
async function _loadEmergency() {
  try {
    _emRows = await _api('/api/harness/emergency/active');
  } catch (e) {
    if (_gate('emergency', e)) return;
    _err('Could not load overrides: ' + (e.message || e));
    return;
  }
  _ungate('emergency');
  const box = _el('harness-em-list');
  box.innerHTML = '';
  if (!_emRows.length) {
    const e = document.createElement('div');
    e.className = 'crew-empty';
    e.textContent = 'No active emergency overrides.';
    box.appendChild(e);
    return;
  }
  for (const o of _emRows) {
    const row = document.createElement('div');
    row.className = 'harness-em-row';
    row.dataset.overrideId = o.id;
    const reason = document.createElement('span');
    reason.className = 'harness-em-reason';
    reason.textContent = o.reason || '(no reason)';
    const meta = document.createElement('span');
    meta.className = 'harness-em-meta';
    meta.textContent = `by ${o.requestedBy} · approved ${o.approvedBy} · ${o.forcedBackend}`;
    const cd = document.createElement('span');
    cd.className = 'harness-countdown';
    cd.dataset.expiresAt = o.expiresAt;
    cd.textContent = _countdownText(o.expiresAt);
    const rv = document.createElement('button');
    rv.type = 'button';
    rv.className = 'crew-btn crew-btn-stop';
    rv.textContent = 'Revoke';
    rv.dataset.act = 'revoke';
    rv.dataset.overrideId = o.id;
    row.append(reason, meta, cd, rv);
    box.appendChild(row);
  }
  _startEmTimer();
}

function _countdownText(expiresAt) {
  const ms = new Date(expiresAt).getTime() - Date.now();
  if (isNaN(ms)) return '—';
  if (ms <= 0) return 'expired';
  const s = Math.floor(ms / 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `expires in ${p(Math.floor(s / 60))}:${p(s % 60)}`;
}
function _startEmTimer() {
  _stopEmTimer();
  _emTimer = setInterval(() => {
    let anyExpired = false;
    document.querySelectorAll('#harness-em-list .harness-countdown').forEach((el) => {
      const t = _countdownText(el.dataset.expiresAt);
      el.textContent = t;
      if (t === 'expired') anyExpired = true;
    });
    if (anyExpired) { _stopEmTimer(); _loadEmergency(); }
  }, 1000);
}
function _stopEmTimer() {
  if (_emTimer) { clearInterval(_emTimer); _emTimer = null; }
}

async function _emCreate() {
  const requestedBy = (_el('harness-em-requestedby').value || '').trim();
  const reason = (_el('harness-em-reason').value || '').trim();
  const ttl = parseInt(_el('harness-em-ttl').value, 10);
  if (!requestedBy || !reason) { _err('requested_by and reason are required'); return; }
  if (isNaN(ttl) || ttl < 1 || ttl > 60) { _err('TTL must be 1–60 minutes'); return; }
  if (!window.confirm(`Activate break-glass for ${ttl} minutes? This forces human_only_emergency and requires a post-mortem.`)) return;
  const btn = _el('harness-em-create');
  btn.disabled = true;
  const msg = _el('harness-em-msg');
  msg.style.display = 'none';
  try {
    await _post('/api/harness/emergency/override', { requested_by: requestedBy, reason, ttl_minutes: ttl });
    _toast('Emergency override active');
    _el('harness-em-reason').value = '';
    await _loadEmergency();
  } catch (e) {
    if (e.status === 403) {
      // Deliberately NOT the whole-panel gate: listing works for any admin,
      // but break-glass needs the security_admin privilege on top.
      msg.className = 'preview-env-warn is-bad';
      msg.textContent = 'security_admin required — normal admin sessions are refused for break-glass (Section 14). Grant the privilege in auth config, then retry.';
      msg.style.display = '';
      return;
    }
    _err('Override failed: ' + (e.message || e));
  } finally {
    btn.disabled = false;
  }
}

async function _emRevoke(id) {
  if (!window.confirm('Revoke this emergency override? The row is deactivated (never overwritten) and still requires a post-mortem.')) return;
  try {
    await _post(`/api/harness/emergency/${encodeURIComponent(id)}/revoke`, {});
    _toast('Override revoked');
    await _loadEmergency();
  } catch (e) {
    if (e.status === 403) {
      const msg = _el('harness-em-msg');
      msg.className = 'preview-env-warn is-bad';
      msg.textContent = 'security_admin required — revoking break-glass needs the same privilege as granting it.';
      msg.style.display = '';
      return;
    }
    _err('Revoke failed: ' + (e.message || e));
  }
}

// --- tabs + overlay --------------------------------------------------------------
const _LOADERS = {
  decisions: _loadAudit,
  registry: _loadRegistry,
  policy: _loadPolicy,
  budget: _loadBudget,
  route: null,               // pure form — nothing to prefetch
  emergency: _loadEmergency,
};

function _showTab(tab) {
  _tab = tab;
  document.querySelectorAll('#harness-tabs .admin-tab').forEach((b) => {
    b.classList.toggle('active', b.dataset.rhtab === tab);
  });
  document.querySelectorAll('#harness-overlay .harness-panel').forEach((p) => {
    p.style.display = p.dataset.rhpanel === tab ? '' : 'none';
  });
  if (tab !== 'emergency') _stopEmTimer();
  const load = _LOADERS[tab];
  if (load && !_loaded[tab]) { _loaded[tab] = true; load(); }
  else if (tab === 'emergency') _startEmTimer();
}

function _openOverlay() {
  const ov = _el('harness-overlay');
  if (!ov) return;
  ov.style.display = '';
  _open = true;
  _loaded = {};              // fresh data every open (cheap, admin-only tool)
  _showTab(_tab);
}
function _closeOverlay() {
  const ov = _el('harness-overlay');
  if (ov) ov.style.display = 'none';
  _open = false;
  _stopEmTimer();
}

const _EXAMPLE_TASK = {
  type: 'diff_review',
  title: 'Review the pending diff',
  objective: 'Review the working-tree diff for correctness and style regressions',
  repoPath: '.',
  risk: 'low',
  routing: { allowFreeModels: true, allowPaidModels: false, allowPremiumModels: false },
};

function refresh() { /* host-wide tool; nothing per-session */ }

function init(apiBase) {
  API_BASE = apiBase || '';
  _el('tool-harness-btn')?.addEventListener('click', _openOverlay);
  _el('harness-close')?.addEventListener('click', _closeOverlay);
  _el('harness-tabs')?.addEventListener('click', (e) => {
    const btn = e.target.closest('.admin-tab[data-rhtab]');
    if (btn) _showTab(btn.dataset.rhtab);
  });
  document.addEventListener('keydown', (e) => {
    const ov = _el('harness-overlay');
    if (e.key === 'Escape' && ov && ov.style.display !== 'none') _closeOverlay();
  });

  // Decisions
  _el('harness-audit-refresh')?.addEventListener('click', _loadAudit);
  _el('harness-audit-filter')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') _loadAudit(); });
  _el('harness-audit-rows')?.addEventListener('click', (e) => {
    const tr = e.target.closest('tr[data-audit-id]');
    if (tr) _selectAudit(tr.dataset.auditId);
  });
  _el('harness-wrap-toggle')?.addEventListener('click', () => {
    const card = _el('harness-wrap-card');
    card.style.display = card.style.display === 'none' ? '' : 'none';
  });
  _el('harness-wrap-submit')?.addEventListener('click', _wrapSubmit);

  // Registry
  _el('harness-registry-refresh')?.addEventListener('click', _loadRegistry);
  _el('harness-registry-newtoggle')?.addEventListener('click', () => {
    const card = _el('harness-registry-newcard');
    card.style.display = card.style.display === 'none' ? '' : 'none';
  });
  _el('harness-reg-create')?.addEventListener('click', _regCreate);
  _el('harness-registry-rows')?.addEventListener('click', (e) => {
    const ctl = e.target.closest('[data-act]');
    if (!ctl) return;
    if (ctl.dataset.act === 'toggle') _regToggle(ctl.dataset.profileId, ctl);
    else if (ctl.dataset.act === 'edit') {
      _regEditing = _regEditing === ctl.dataset.profileId ? null : ctl.dataset.profileId;
      _loadRegistry();
    } else if (ctl.dataset.act === 'delete') _regDelete(ctl.dataset.profileId);
  });

  // Policy
  _el('harness-policy-publish')?.addEventListener('click', _policyPublish);
  _el('harness-policy-reload')?.addEventListener('click', _loadPolicy);
  _el('harness-policy-versions')?.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-archive]');
    if (btn) _policyRollback(btn.dataset.archive);
  });

  // Budget + Route preview
  _el('harness-budget-preview')?.addEventListener('click', _budgetPreview);
  _el('harness-route-preview')?.addEventListener('click', _routePreview);
  const example = JSON.stringify(_EXAMPLE_TASK, null, 2);
  if (_el('harness-budget-task')) _el('harness-budget-task').value = example;
  if (_el('harness-route-task')) _el('harness-route-task').value = example;

  // Emergency
  _el('harness-em-refresh')?.addEventListener('click', _loadEmergency);
  _el('harness-em-create')?.addEventListener('click', _emCreate);
  _el('harness-em-list')?.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-act="revoke"]');
    if (btn) _emRevoke(btn.dataset.overrideId);
  });
}

const routingHarnessModule = { init, refresh };
export default routingHarnessModule;
window.routingHarnessModule = routingHarnessModule;
