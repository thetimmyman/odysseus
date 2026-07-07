// static/js/crewPanel.js — the Argo voyage-log panel (roadmap Tier 2).
//
// A live + historical view of agent-crew "voyages": launch a read-only or
// write-mode quest, watch Athena + the Argonauts stream as a multi-agent
// timeline, reconnect to an in-flight run, stop it, and answer the Oracle's
// seal (the async approval gate) right here.
//
// Mirrors gitPanel.js / terminal.js: a sidebar section that opens an overlay,
// talks only to /api/crew/* (admin-cookie + owner-scoped server-side), and is
// XSS-safe (textContent / _esc for every server/model string). The crew SSE is
// consumed with fetch()+ReadableStream (POST can't use EventSource); the
// historical log reads the DB via GET (the SSE buffer is evicted after 180s).
//
// Contract: homelab/odysseus CREW-EVENT-CONTRACT.md.

let API_BASE = '';
let _curSession = null;
let _open = false;
let _reader = null;        // active stream reader (so we can cancel on close/switch)
let _streamGen = 0;        // generation token; a stale reader checks this and bails
let _voyage = null;        // current in-view voyage state
let _runsTimer = null;     // poll timer for the runs list

// --- helpers -----------------------------------------------------------------
function _esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
function _toast(msg) {
  if (window.uiModule && window.uiModule.showToast) window.uiModule.showToast(msg);
}
function _err(msg) {
  if (window.uiModule && window.uiModule.showError) window.uiModule.showError(msg);
  else _toast(msg);
}
function _el(id) { return document.getElementById(id); }
function _sessionId() {
  const sm = window.sessionModule;
  if (sm && sm.getCurrentSessionId) return sm.getCurrentSessionId();
  return _curSession;
}
function _relTime(iso) {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (isNaN(t)) return '';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}

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

// --- SSE frame parser (data: {json}\n\n, plus event: error\ndata: ... form) --
function _parseFrames(buf, onEvent) {
  let idx;
  while ((idx = buf.indexOf('\n\n')) >= 0) {
    const frame = buf.slice(0, idx);
    buf = buf.slice(idx + 2);
    let ev = null;
    const dataLines = [];
    for (const line of frame.split('\n')) {
      if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''));
      else if (line.startsWith('event:')) ev = line.slice(6).trim();
    }
    if (!dataLines.length) continue;
    const data = dataLines.join('\n');
    if (data === '[DONE]') { onEvent({ __done: true }); continue; }
    let obj;
    try { obj = JSON.parse(data); } catch { continue; }
    if (ev) obj.__event = ev;
    onEvent(obj);
  }
  return buf;
}

// --- voyage state ------------------------------------------------------------
function _newVoyage(runId, writeMode, prompt) {
  return {
    runId, writeMode: !!writeMode, prompt: prompt || '',
    status: 'running', plan: null, result: null, error: null,
    agents: new Map(),     // agent_id -> lane state {role, model, subtask, ...DOM refs}
    approvals: new Map(),  // approval_id -> {tool, risk, agent_id, resolved}
  };
}

function _statusBadge(status) {
  const s = String(status || '').toLowerCase();
  let cls = 'crew-st-run', label = s || 'running';
  if (s === 'success') { cls = 'crew-st-ok'; }
  else if (s === 'error') { cls = 'crew-st-err'; }
  else if (s === 'stopped') { cls = 'crew-st-stop'; }
  else if (s === 'blocked') { cls = 'crew-st-block'; }
  return `<span class="crew-badge ${cls}">${_esc(label)}</span>`;
}

// --- timeline rendering ------------------------------------------------------
function _timeline() { return _el('crew-timeline'); }

function _autoscroll() {
  const tl = _timeline();
  if (!tl) return;
  // Only stick to the bottom if the user is already near it.
  if (tl.scrollHeight - tl.scrollTop - tl.clientHeight < 120) {
    tl.scrollTop = tl.scrollHeight;
  }
}

function _ensureLane(agentId, role, roleKind) {
  if (!_voyage) return null;
  let lane = _voyage.agents.get(agentId);
  if (lane) {
    if (role && !lane.role) lane.role = role;
    return lane;
  }
  const tl = _timeline();
  if (!tl) return null;
  const isAthena = agentId === 'athena' || (roleKind === 'planner');

  const card = document.createElement('div');
  card.className = 'crew-lane' + (isAthena ? ' crew-lane-athena' : '');
  card.dataset.agent = agentId;

  const head = document.createElement('div');
  head.className = 'crew-lane-head';
  const name = document.createElement('span');
  name.className = 'crew-lane-name';
  name.textContent = role || agentId;
  const meta = document.createElement('span');
  meta.className = 'crew-lane-meta';
  const stat = document.createElement('span');
  stat.className = 'crew-lane-status';
  head.appendChild(name);
  head.appendChild(meta);
  head.appendChild(stat);
  card.appendChild(head);

  const sub = document.createElement('div');
  sub.className = 'crew-lane-subtask';
  sub.style.display = 'none';
  card.appendChild(sub);

  // collapsible reasoning (thinking:true deltas)
  const think = document.createElement('details');
  think.className = 'crew-thinking';
  think.style.display = 'none';
  const tsum = document.createElement('summary');
  tsum.textContent = 'Reasoning';
  const tbody = document.createElement('div');
  tbody.className = 'crew-thinking-body';
  think.appendChild(tsum);
  think.appendChild(tbody);
  card.appendChild(think);

  const out = document.createElement('div');
  out.className = 'crew-lane-output';
  card.appendChild(out);

  const steps = document.createElement('div');
  steps.className = 'crew-lane-steps';
  card.appendChild(steps);

  const foot = document.createElement('div');
  foot.className = 'crew-lane-foot';
  foot.style.display = 'none';
  card.appendChild(foot);

  tl.appendChild(card);

  lane = {
    role: role || agentId, roleKind, isAthena,
    card, meta, stat, sub, think, tbody, out, steps, foot,
    outText: '', thinkText: '', rounds: 0, finalShown: false,
  };
  _voyage.agents.set(agentId, lane);
  return lane;
}

function _setLaneStatus(lane, status) {
  if (!lane) return;
  lane.stat.innerHTML = _statusBadge(status);
}

function _onDelta(ev) {
  const lane = _ensureLane(ev.agent_id, ev.role);
  if (!lane) return;
  const piece = ev.delta || '';
  if (ev.thinking) {
    lane.thinkText += piece;
    lane.tbody.textContent = lane.thinkText;
    lane.think.style.display = '';
  } else {
    lane.outText += piece;
    lane.out.textContent = lane.outText;
  }
  _autoscroll();
}

function _onAgentStart(ev) {
  // The athena start carries the unguessable crew_run_id — capture it so Stop,
  // approvals, and the runs-list selection can target this voyage.
  if (ev.crew_run_id && _voyage && !_voyage.runId) {
    _voyage.runId = ev.crew_run_id;
    _updateRunControls();
    _loadRuns();
  }
  const lane = _ensureLane(ev.agent_id, ev.role, ev.role_kind);
  if (!lane) return;
  _setLaneStatus(lane, 'running');
  const bits = [];
  if (ev.model) bits.push(_esc(ev.model));
  lane.meta.innerHTML = bits.join(' · ');
  if (ev.subtask) {
    lane.sub.textContent = ev.subtask;
    lane.sub.style.display = '';
  }
  if (ev.write_mode) {
    _voyage.writeMode = true;
  }
}

function _onPlan(ev) {
  if (!_voyage) return;
  _voyage.plan = ev.subtasks || [];
  const lane = _ensureLane('athena', 'Athena', 'planner');
  if (!lane) return;
  const wrap = document.createElement('div');
  wrap.className = 'crew-plan';
  const h = document.createElement('div');
  h.className = 'crew-plan-h';
  h.textContent = 'Plan';
  wrap.appendChild(h);
  const ol = document.createElement('ol');
  for (const t of _voyage.plan) {
    const li = document.createElement('li');
    li.textContent = t;
    ol.appendChild(li);
  }
  wrap.appendChild(ol);
  lane.out.appendChild(wrap);
  _autoscroll();
}

function _onBlocked(ev) {
  const lane = _ensureLane('athena', 'Athena', 'planner');
  if (!lane) return;
  const n = document.createElement('div');
  n.className = 'crew-blocked';
  n.textContent = 'Blocked: ' + (ev.reason || 'unknown')
    + (ev.tokens_used ? ` (${ev.tokens_used} tok)` : '');
  lane.out.appendChild(n);
  _autoscroll();
}

function _onStep(ev) {
  const lane = _ensureLane(ev.agent_id, ev.role);
  if (!lane) return;
  if (ev.round) lane.rounds = Math.max(lane.rounds, ev.round | 0);
  if (!ev.tool) return;       // bare agent_step round ticks: no row
  const row = document.createElement('div');
  row.className = 'crew-step';
  const tool = document.createElement('span');
  tool.className = 'crew-step-tool';
  tool.textContent = ev.tool;
  row.appendChild(tool);
  if (ev.command) {
    const cmd = document.createElement('span');
    cmd.className = 'crew-step-cmd';
    cmd.textContent = String(ev.command).slice(0, 200);
    row.appendChild(cmd);
  }
  lane.steps.appendChild(row);
  _autoscroll();
}

function _onMetrics(ev) {
  const lane = _voyage && _voyage.agents.get(ev.agent_id);
  if (!lane) return;
  const d = ev.data || {};
  const bits = [];
  if (d.total_tokens) bits.push(`${d.total_tokens} tok`);
  if (d.tokens_per_second) bits.push(`${Math.round(d.tokens_per_second)} t/s`);
  if (d.model) bits.push(_esc(d.model));
  lane.foot.innerHTML = bits.join(' · ');
  if (bits.length) lane.foot.style.display = '';
}

function _onAgentFinal(ev) {
  const lane = _ensureLane(ev.agent_id, ev.role);
  if (!lane) return;
  _setLaneStatus(lane, ev.status || 'success');
  lane.finalShown = true;
  // If the worker streamed nothing, show its result text as the output body.
  if (!lane.outText.trim() && ev.result) {
    lane.out.textContent = ev.result;
  }
  if (ev.rounds) {
    const r = document.createElement('span');
    r.className = 'crew-lane-rounds';
    r.textContent = ` · ${ev.rounds} rounds`;
    lane.stat.appendChild(r);
  }
  _autoscroll();
}

function _onDone(ev) {
  if (!_voyage) return;
  _voyage.status = ev.status || 'success';
  _voyage.result = ev.result || null;
  if (ev.error) _voyage.error = ev.error;
  const lane = _ensureLane('athena', 'Athena', 'planner');
  if (lane && ev.result) {
    const wrap = document.createElement('div');
    wrap.className = 'crew-synthesis';
    const h = document.createElement('div');
    h.className = 'crew-synthesis-h';
    h.textContent = "Athena's synthesis";
    const body = document.createElement('div');
    body.className = 'crew-synthesis-body';
    body.textContent = ev.result;
    wrap.appendChild(h);
    wrap.appendChild(body);
    lane.out.appendChild(wrap);
  }
  if (lane) _setLaneStatus(lane, _voyage.status);
  if (ev.error && lane) {
    const e = document.createElement('div');
    e.className = 'crew-blocked';
    e.textContent = 'Error: ' + ev.error;
    lane.out.appendChild(e);
  }
  _updateRunControls();
  _autoscroll();
  // Refresh the runs list so the new voyage's terminal status shows.
  _loadRuns();
}

// --- approvals (the Oracle's seal) -------------------------------------------
function _approvalsBox() { return _el('crew-approvals'); }

async function _onApprovalRequest(ev) {
  if (!_voyage) return;
  if (_voyage.approvals.has(ev.approval_id)) return;
  _voyage.approvals.set(ev.approval_id, { tool: ev.tool, risk: ev.risk, resolved: false });
  // The SSE event omits action_args (redacted-at-rest in the DB) — fetch them.
  let args = null;
  try {
    const j = await _api(`/api/crew/run/${encodeURIComponent(_voyage.runId)}/approvals`);
    const found = (j.approvals || []).find((a) => a.id === ev.approval_id);
    if (found) args = found.action_args;
  } catch { /* show without args */ }
  _renderApproval(ev.approval_id, ev.tool, ev.risk, ev.agent_id, args);
}

function _renderApproval(approvalId, tool, risk, agentId, args) {
  const box = _approvalsBox();
  if (!box) return;
  const card = document.createElement('div');
  card.className = 'crew-approval';
  card.dataset.approval = approvalId;

  const h = document.createElement('div');
  h.className = 'crew-approval-h';
  h.innerHTML = '⚓ <strong>The Oracle’s seal</strong> — '
    + _esc(agentId || 'a worker') + ' wants to run '
    + `<code>${_esc(tool)}</code>`
    + (risk ? ` <span class="crew-risk crew-risk-${_esc(risk)}">${_esc(risk)}</span>` : '');
  card.appendChild(h);

  if (args) {
    const pre = document.createElement('pre');
    pre.className = 'crew-approval-args';
    pre.textContent = typeof args === 'string' ? args : JSON.stringify(args, null, 2);
    card.appendChild(pre);
  }

  const actions = document.createElement('div');
  actions.className = 'crew-approval-actions';
  const approve = document.createElement('button');
  approve.type = 'button';
  approve.className = 'crew-btn crew-btn-approve';
  approve.textContent = 'Approve';
  const reject = document.createElement('button');
  reject.type = 'button';
  reject.className = 'crew-btn crew-btn-reject';
  reject.textContent = 'Reject';
  approve.addEventListener('click', () => _decide(approvalId, 'approved', card));
  reject.addEventListener('click', () => _decide(approvalId, 'rejected', card));
  actions.appendChild(approve);
  actions.appendChild(reject);
  card.appendChild(actions);

  box.appendChild(card);
  box.style.display = '';
}

async function _decide(approvalId, decision, card) {
  const btns = card.querySelectorAll('button');
  btns.forEach((b) => { b.disabled = true; });
  try {
    await _api('/api/crew/approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approval_id: approvalId, decision }),
    });
    const a = _voyage && _voyage.approvals.get(approvalId);
    if (a) a.resolved = true;
    card.classList.add('crew-approval-done', decision === 'approved' ? 'is-approved' : 'is-rejected');
    const tag = document.createElement('div');
    tag.className = 'crew-approval-outcome';
    tag.textContent = decision === 'approved' ? 'Sealed ✓' : 'Refused ✕';
    card.appendChild(tag);
    // Hide the box once nothing is pending.
    setTimeout(() => {
      card.remove();
      const box = _approvalsBox();
      if (box && !box.querySelector('.crew-approval')) box.style.display = 'none';
    }, 2500);
  } catch (e) {
    btns.forEach((b) => { b.disabled = false; });
    if (e.status === 409) _err('That approval was already decided.');
    else if (e.status === 403) _err('Approvals need the desktop admin session.');
    else _err('Approve failed: ' + (e.message || e));
  }
}

// --- stream dispatch ---------------------------------------------------------
function _dispatch(ev) {
  if (ev.__done) return;
  const t = ev.type;
  if (ev.__event === 'error') {
    if (_voyage) { _voyage.status = 'error'; _voyage.error = ev.error || ev.message || 'stream error'; }
    const lane = _ensureLane('athena', 'Athena', 'planner');
    if (lane) {
      const e = document.createElement('div');
      e.className = 'crew-blocked';
      e.textContent = 'Error: ' + (ev.error || ev.message || 'stream error');
      lane.out.appendChild(e);
    }
    _updateRunControls();
    return;
  }
  if (t === 'crew_agent_start') return _onAgentStart(ev);
  if (t === 'crew_handoff') return;                     // implicit in lane order
  if (t === 'agent_prep') return;                       // timing telemetry, ignore
  if (t === 'crew_step') {
    if (ev.phase === 'planned') return _onPlan(ev);
    if (ev.phase === 'blocked') return _onBlocked(ev);
    return;
  }
  if (t === 'crew_agent_output') {
    if ('delta' in ev) return _onDelta(ev);
    if ('result' in ev) return _onAgentFinal(ev);
    return;
  }
  if (t === 'crew_agent_step' || t === 'agent_step') return _onStep(ev);
  if (t === 'metrics') return _onMetrics(ev);
  if (t === 'crew_approval_request') return _onApprovalRequest(ev);
  if (t === 'crew_done') return _onDone(ev);
}

async function _consume(resp, gen) {
  const reader = resp.body.getReader();
  _reader = reader;
  const dec = new TextDecoder();
  let buf = '';
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      if (gen !== _streamGen) { try { reader.cancel(); } catch { /* noop */ } return; }
      buf += dec.decode(value, { stream: true });
      buf = _parseFrames(buf, _dispatch);
    }
  } catch (e) {
    if (gen === _streamGen && _voyage && _voyage.status === 'running') {
      // network drop mid-run — surface a soft notice, leave reconnect to user
      const lane = _ensureLane('athena', 'Athena', 'planner');
      if (lane) {
        const n = document.createElement('div');
        n.className = 'crew-blocked';
        n.textContent = 'Stream interrupted — reopen this voyage to reconnect.';
        lane.out.appendChild(n);
      }
    }
  } finally {
    if (_reader === reader) _reader = null;
  }
  if (gen === _streamGen) _updateRunControls();
}

// --- launch / reconnect / historical -----------------------------------------
function _resetTimeline(prompt, writeMode) {
  const tl = _timeline();
  if (tl) tl.innerHTML = '';
  const box = _approvalsBox();
  if (box) { box.innerHTML = ''; box.style.display = 'none'; }
  const banner = _el('crew-voyage-prompt');
  if (banner) {
    banner.textContent = prompt || '';
    banner.style.display = prompt ? '' : 'none';
  }
}

async function _launch() {
  const ta = _el('crew-prompt');
  const wm = _el('crew-writemode');
  const prompt = (ta && ta.value || '').trim();
  if (!prompt) { _err('Enter a quest for Athena'); return; }
  const writeMode = !!(wm && wm.checked);
  _streamGen += 1;
  const gen = _streamGen;
  _voyage = _newVoyage(null, writeMode, prompt);
  _resetTimeline(prompt, writeMode);
  _setLaunchBusy(true);
  try {
    const body = { prompt, write_mode: writeMode };
    const sid = _sessionId();
    if (sid) body.session_id = sid;        // give the crew a project_root
    const r = await fetch(`${API_BASE}/api/crew/run`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      let detail = `${r.status}`;
      try { const j = await r.json(); if (j && j.detail) detail = j.detail; } catch { /* noop */ }
      if (r.status === 403) detail = 'Launching a voyage needs the desktop admin session.';
      throw new Error(detail);
    }
    // The crew_run_id arrives in the first crew_agent_start (athena) event and
    // is captured in _onAgentStart via ev.crew_run_id.
    if (ta) ta.value = '';
    await _consume(r, gen);
  } catch (e) {
    _err('Launch failed: ' + (e.message || e));
  } finally {
    _setLaunchBusy(false);
    _updateRunControls();
    _loadRuns();
  }
}

async function _openRun(runId, status) {
  _streamGen += 1;
  const gen = _streamGen;
  if (_reader) { try { _reader.cancel(); } catch { /* noop */ } _reader = null; }
  const isRunning = String(status || '').toLowerCase() === 'running';
  if (isRunning) {
    // Live (or <180s) run: stream with full replay → rebuild from scratch.
    _voyage = _newVoyage(runId, false, '');
    _resetTimeline('', false);
    _setLaunchBusy(false);
    try {
      const r = await fetch(`${API_BASE}/api/crew/run/${encodeURIComponent(runId)}/stream`, {
        credentials: 'same-origin', headers: { Accept: 'text/event-stream' },
      });
      if (!r.ok) throw new Error(`${r.status}`);
      await _consume(r, gen);
    } catch (e) {
      _err('Could not reconnect: ' + (e.message || e));
    } finally {
      _updateRunControls();
    }
  } else {
    // Finished / evicted: render the stored detail statically.
    await _openHistorical(runId);
  }
}

async function _openHistorical(runId) {
  _voyage = _newVoyage(runId, false, '');
  _resetTimeline('', false);
  try {
    const d = await _api(`/api/crew/run/${encodeURIComponent(runId)}`);
    _voyage.status = d.status;
    _voyage.prompt = d.prompt || '';
    _voyage.writeMode = false;
    const banner = _el('crew-voyage-prompt');
    if (banner) { banner.textContent = d.prompt || ''; banner.style.display = d.prompt ? '' : 'none'; }
    // Athena lane: plan + synthesis.
    const ath = _ensureLane('athena', 'Athena', 'planner');
    if (ath) {
      _setLaneStatus(ath, d.status);
      if (Array.isArray(d.plan) && d.plan.length) {
        _onPlan({ subtasks: d.plan.map((p) => (p && (p.title || p)) || '') });
      }
    }
    // Worker lanes from CrewAgentRun rows.
    for (const a of (d.agents || [])) {
      const lane = _ensureLane(a.agent_id, a.role || a.agent_id, a.role && /planner/i.test(a.role) ? 'planner' : 'worker');
      if (!lane) continue;
      if (a.model) lane.meta.textContent = a.model;
      if (a.subtask) { lane.sub.textContent = a.subtask; lane.sub.style.display = ''; }
      if (a.result) lane.out.textContent = a.result;
      else if (a.error) { const e = document.createElement('div'); e.className = 'crew-blocked'; e.textContent = 'Error: ' + a.error; lane.out.appendChild(e); }
      _setLaneStatus(lane, a.status || 'success');
      const fb = [];
      if (a.tokens_used) fb.push(`${a.tokens_used} tok`);
      if (a.rounds) fb.push(`${a.rounds} rounds`);
      if (a.model) fb.push(a.model);
      if (fb.length) { lane.foot.textContent = fb.join(' · '); lane.foot.style.display = ''; }
    }
    // Synthesis / error.
    if (ath) {
      if (d.result) _onDone({ status: d.status, result: d.result });
      else if (d.error) { const e = document.createElement('div'); e.className = 'crew-blocked'; e.textContent = 'Error: ' + d.error; ath.out.appendChild(e); }
    }
  } catch (e) {
    _err('Could not load voyage: ' + (e.message || e));
  } finally {
    _updateRunControls();
  }
}

async function _stop() {
  if (!_voyage || !_voyage.runId) return;
  const btn = _el('crew-stop');
  if (btn) btn.disabled = true;
  try {
    await _api(`/api/crew/run/${encodeURIComponent(_voyage.runId)}/stop`, { method: 'POST' });
    _toast('Voyage stopping…');
  } catch (e) {
    if (e.status === 403) _err('Stopping needs the desktop admin session.');
    else _err('Stop failed: ' + (e.message || e));
    if (btn) btn.disabled = false;
  }
}

// --- runs list (left rail) ---------------------------------------------------
async function _loadRuns() {
  const list = _el('crew-runs');
  if (!list) return;
  try {
    const j = await _api('/api/crew/runs?limit=40');
    const runs = j.runs || [];
    list.innerHTML = '';
    if (!runs.length) {
      const empty = document.createElement('div');
      empty.className = 'crew-empty';
      empty.textContent = 'No voyages yet. Launch one above.';
      list.appendChild(empty);
      return;
    }
    for (const run of runs) {
      const row = document.createElement('div');
      row.className = 'crew-run-row';
      if (_voyage && _voyage.runId === run.id) row.classList.add('selected');
      row.dataset.runId = run.id;
      row.dataset.status = run.status;
      const top = document.createElement('div');
      top.className = 'crew-run-top';
      top.innerHTML = _statusBadge(run.status);
      const when = document.createElement('span');
      when.className = 'crew-run-when';
      when.textContent = _relTime(run.started_at);
      top.appendChild(when);
      row.appendChild(top);
      const p = document.createElement('div');
      p.className = 'crew-run-prompt';
      p.textContent = run.prompt || '(no prompt)';
      p.title = run.prompt || '';
      row.appendChild(p);
      list.appendChild(row);
    }
  } catch (e) {
    if (e.status === 403) { list.innerHTML = '<div class="crew-empty">Sign in to view voyages.</div>'; return; }
    list.innerHTML = `<div class="crew-empty">Could not load voyages (${_esc(e.message || e)})</div>`;
  }
}

// --- controls / overlay ------------------------------------------------------
function _setLaunchBusy(busy) {
  const btn = _el('crew-launch');
  if (btn) { btn.disabled = busy; btn.textContent = busy ? 'Sailing…' : 'Launch voyage'; }
}
function _updateRunControls() {
  const stop = _el('crew-stop');
  const running = !!(_voyage && _voyage.status === 'running' && _voyage.runId);
  if (stop) stop.style.display = running ? '' : 'none';
}

function _openOverlay() {
  const ov = _el('crew-overlay');
  if (!ov) return;
  ov.style.display = '';
  _open = true;
  _loadRuns();
  if (_runsTimer) clearInterval(_runsTimer);
  _runsTimer = setInterval(() => { if (_open) _loadRuns(); }, 8000);
}
function _closeOverlay() {
  const ov = _el('crew-overlay');
  if (ov) ov.style.display = 'none';
  _open = false;
  _streamGen += 1;                  // invalidate any live reader
  if (_reader) { try { _reader.cancel(); } catch { /* noop */ } _reader = null; }
  if (_runsTimer) { clearInterval(_runsTimer); _runsTimer = null; }
}

// --- public ------------------------------------------------------------------
function refresh(sessionId) {
  // The Argo opens from the Tools menu now — just track the active session so a
  // launched voyage inherits its project_root.
  _curSession = sessionId;
}

function init(apiBase) {
  API_BASE = apiBase || '';

  _el('tool-argo-btn')?.addEventListener('click', _openOverlay);
  _el('crew-close')?.addEventListener('click', _closeOverlay);
  _el('crew-launch')?.addEventListener('click', _launch);
  _el('crew-stop')?.addEventListener('click', _stop);
  _el('crew-runs')?.addEventListener('click', (e) => {
    const row = e.target.closest('.crew-run-row');
    if (row) _openRun(row.dataset.runId, row.dataset.status);
  });
  // Ctrl/Cmd+Enter in the composer launches.
  _el('crew-prompt')?.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); _launch(); }
  });
  // Esc closes the overlay.
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && _open) _closeOverlay();
  });
}

const crewPanelModule = { init, refresh };
export default crewPanelModule;
window.crewPanelModule = crewPanelModule;
