const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const { pathToFileURL } = require('node:url');
const vm = require('node:vm');

// Execute complete application modules. Only browser APIs and unrelated
// imported services are fake; workspace/session/socket behavior is not copied.
async function harness({ realRenderer = false, realChat = false } = {}) {
  const elements = new Map();
  const calls = { fetch: [], errors: [], toasts: [], focus: [], rendered: [], consoleErrors: [] };
  class Element {
    constructor(tag = 'div', id = '') {
      this.tagName = tag.toUpperCase();
      this.id = id;
      this.style = { setProperty(name, value) { this[name] = value; } };
      this.dataset = {};
      this.value = '';
      this.textContent = '';
      this.disabled = false;
      this.children = [];
      this.events = new Map();
      const classes = new Set();
      this.classes = classes;
      this.classList = {
        add: (...names) => names.forEach((name) => classes.add(name)),
        remove: (...names) => names.forEach((name) => classes.delete(name)),
        contains: (name) => classes.has(name),
        toggle(name, force = !classes.has(name)) {
          if (force) classes.add(name); else classes.delete(name);
          return force;
        },
      };
    }
    set className(value) { this.classes.clear(); value.split(/\s+/).filter(Boolean).forEach((name) => this.classes.add(name)); }
    get className() { return [...this.classes].join(' '); }
    set innerHTML(value) {
      this.html = value;
      this.children = [];
      // The picker creates its existing markup dynamically. Materialize only
      // its controls, so tests dispatch the real registered button handlers.
      for (const match of value.matchAll(/<([a-z]+)\b([^>]*)>/gi)) {
        const id = match[2].match(/\bid="([^"]+)"/)?.[1];
        const names = match[2].match(/\bclass="([^"]+)"/)?.[1];
        if (!id && !names) continue;
        const child = new Element(match[1], id || '');
        if (names) child.classList.add(...names.split(/\s+/));
        const path = match[2].match(/\bdata-path="([^"]+)"/)?.[1];
        if (path) child.dataset.path = path;
        this.appendChild(child);
      }
    }
    get innerHTML() { return this.html || ''; }
    appendChild(child) {
      this.children.push(child);
      if (child.id) elements.set(child.id, child);
      return child;
    }
    querySelectorAll(selector) {
      const matches = (child) => selector.startsWith('#')
        ? child.id === selector.slice(1)
        : selector.startsWith('.') && child.classList.contains(selector.slice(1));
      return this.children.flatMap((child) => [
        ...(matches(child) ? [child] : []), ...child.querySelectorAll(selector),
      ]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    addEventListener(type, handler) {
      if (!this.events.has(type)) this.events.set(type, []);
      this.events.get(type).push(handler);
    }
    async dispatch(type, extra = {}) {
      const event = { target: this, currentTarget: this, preventDefault() {}, ...extra };
      for (const handler of this.events.get(type) || []) await handler(event);
      await settle();
    }
    dispatchEvent(event) { for (const handler of this.events.get(event.type) || []) handler(event); }
    setAttribute(name, value) { this[name] = value; }
    removeAttribute(name) { delete this[name]; }
    focus() { calls.focus.push(this.id); }
  }
  const body = new Element('body');
  for (const id of [
    'workspace-indicator-btn', 'workspace-indicator-name', 'overflow-workspace-btn',
    'message', 'chat-history', 'current-meta', 'sessions-section',
    'terminal-overlay', 'terminal-xterm', 'terminal-status', 'tool-terminal-btn',
    'terminal-close', 'terminal-connect-btn', 'terminal-disconnect-btn', 'send-btn',
  ]) body.appendChild(new Element('div', id));
  elements.get('send-btn').classList.add('send-btn');
  elements.get('terminal-overlay').style.display = 'none';
  const document = {
    body, head: new Element('head'), readyState: 'loading',
    getElementById: (id) => elements.get(id) || null,
    // Unrelated session-list rendering and dropdown geometry are outside this
    // suite. The real session-selection code still renders history below.
    querySelector: (selector) => selector === '.send-btn' ? elements.get('send-btn') : null,
    querySelectorAll: () => [],
    createElement: (tag) => new Element(tag),
    addEventListener() {}, dispatchEvent() {},
  };
  const values = new Map();
  const Storage = {
    get: (key, fallback = null) => values.has(key) ? values.get(key) : fallback,
    set: (key, value) => values.set(key, value),
    remove: (key) => values.delete(key),
    getJSON: (key, fallback) => values.has(key) ? JSON.parse(values.get(key)) : fallback,
    setJSON: (key, value) => values.set(key, JSON.stringify(value)),
  };
  const webStorage = () => {
    const data = new Map();
    return { getItem: (key) => data.get(key) ?? null,
      setItem: (key, value) => data.set(key, String(value)),
      removeItem: (key) => data.delete(key) };
  };
  const sessionStorage = webStorage();
  sessionStorage.setItem('ody-session-active', '1');
  const sockets = [];
  class WebSocket {
    static OPEN = 1;
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.sent = [];
      this.closeCount = 0;
      sockets.push(this);
    }
    open() { this.readyState = WebSocket.OPEN; this.onopen?.(); }
    send(data) { this.sent.push(JSON.parse(data)); }
    close() {
      this.closeCount++;
      this.readyState = 3;
      this.onclose?.({ code: 1000 });
    }
    input() { return this.sent.filter((message) => message.type === 'input'); }
  }
  const terminals = [];
  class Terminal {
    constructor() { this.cols = 80; this.rows = 24; terminals.push(this); }
    loadAddon() {} open() {} focus() {}
    onData(handler) { this.data = handler; }
    onResize(handler) { this.resize = handler; }
    write(value) { calls.rendered.push(value); }
  }
  const location = { origin: 'https://assistant.example', protocol: 'https:',
    host: 'assistant.example', pathname: '/', hash: '' };
  const window = {
    location, innerWidth: 1440, addEventListener() {}, Terminal,
    FitAddon: { FitAddon: class { fit() {} } },
    presetsModule: { onSessionSwitch() {} },
    chatModule: {
      detachCurrentStream() {}, showWelcomeScreen() {},
      addMessage: (...args) => calls.rendered.push(args),
    },
  };
  let fetchHandler = () => { throw new Error('Unexpected fetch'); };
  const context = vm.createContext({
    window, document, navigator: { platform: 'Linux' },
    sessionStorage, localStorage: webStorage(), URLSearchParams, FormData, URL,
    Event, AbortController, TextDecoder,
    CustomEvent: class { constructor(type) { this.type = type; } },
    MutationObserver: class { observe() {} disconnect() {} },
    WebSocket, console: { ...console,
      error: (...args) => calls.consoleErrors.push(args.map(String).join(' ')) },
    history: { replaceState(_state, _title, url) {
      location.hash = url.startsWith('#') ? url : '';
    } },
    // Resolve history fades, but do not run unrelated background timers.
    setTimeout: (fn, delay) => { if (delay === 120) queueMicrotask(fn); return 1; },
    clearTimeout() {}, setInterval: () => 1, clearInterval() {},
    requestAnimationFrame: () => 1, cancelAnimationFrame() {},
    fetch: (url, options = {}) => {
      calls.fetch.push({ url, options });
      return fetchHandler(url, options);
    },
  });
  const ui = {
    el: (id) => document.getElementById(id), esc: String,
    showError: (message) => calls.errors.push(message),
    showToast: (message) => calls.toasts.push(message),
    scrollHistoryInstant() {}, scrollHistory() {}, setAutoScroll() {},
  };
  const stubs = {
    'storage.js': { default: Storage, KEYS: { WORKSPACE: 'fixture-workspace' } },
    'ui.js': { default: ui, styledPrompt: async () => null,
      focusWindow: (element) => calls.focus.push(element.id) },
    'windowDrag.js': { makeWindowDraggable() {} },
    'markdown.js': { default: { renderContent: (value) => value,
      processWithThinking: (value) => value, squashOutsideCode: (value) => value }, svgifyEmoji: (value) => value },
    'chatRenderer.js': { default: { updateSessionCostUI() {},
      addMessage: (...args) => { calls.rendered.push(args); return new Element(); },
      showWelcomeScreen() {}, hideWelcomeScreen() {} } },
    'providers.js': { providerLogo: () => '', providerLabel: (value) => value },
    'modelPicker.js': { initModelPicker() {}, updateModelPicker() {} },
    'theme.js': { default: {} }, 'spinner.js': { default: {} },
    'settings.js': { default: {} }, 'tts-ai.js': { addAITTSButton() {} },
    'escMenuStack.js': { bindMenuDismiss() {} }, 'matchKey.js': { matchModelKey: () => null },
    'chatStream.js': { default: {} }, 'planWindow.js': { default: {} },
    'presets.js': { default: {} },
    'fileHandler.js': { default: { getPendingCount: () => 0 } },
    'search.js': { default: {} }, 'document.js': { default: { getSelectionContext: () => null } },
    'emailInbox.js': { init() {} }, 'codeRunner.js': { default: {} },
    'slashCommands.js': { default: { getSetupMode: () => null, clearSetupMode() {} },
      initSlashCommands() {}, isCommand: () => false, handleSlashCommand: async () => false,
      handleSetupInput() {}, handleSetupWizard() {}, typewriterInto() {} },
    'researchSynapse.js': { default() {} }, 'streamingRenderer.js': { createStreamRenderer() {} },
    'composerArrowUpRecall.js': { wireArrowUpRecall: () => true, getLastUserMessageFromChatHistory: () => null },
  };
  if (realRenderer) delete stubs['chatRenderer.js'];
  const modules = new Map();
  async function load(url) {
    if (modules.has(url.href)) return modules.get(url.href);
    const stub = stubs[url.pathname.split('/').pop()];
    const module = stub
      ? new vm.SyntheticModule(Object.keys(stub), function () {
        for (const [name, value] of Object.entries(stub)) this.setExport(name, value);
      }, { context, identifier: url.href })
      : new vm.SourceTextModule(readFileSync(url, 'utf8'), { context, identifier: url.href });
    modules.set(url.href, module);
    await module.link((specifier, owner) => load(new URL(specifier, owner.identifier)));
    return module;
  }
  const base = pathToFileURL(resolve(__dirname, '../../static/js') + '/');
  const workspaceModule = await load(new URL('workspace.js', base));
  await workspaceModule.evaluate();
  const workspace = workspaceModule.namespace.default;
  const sessionModule = await load(new URL('sessions.js', base));
  await sessionModule.evaluate();
  const sessions = sessionModule.namespace.default;
  window.sessionModule = sessions;
  const terminalModule = await load(new URL('terminal.js', base));
  await terminalModule.evaluate();
  const terminal = terminalModule.namespace.default;
  terminal.init(location.origin);
  let chat = null;
  if (realChat) {
    const chatModule = await load(new URL('chat.js', base));
    await chatModule.evaluate();
    chat = chatModule.namespace.default;
  }
  const renderer = modules.get(new URL('chatRenderer.js', base).href).namespace.default;
  return { workspace, sessions, terminal, chat, renderer, elements, Storage, calls, sockets, terminals,
    fetchWith(handler) { fetchHandler = handler; },
    select(id, path = '') {
      const meta = { id, name: id, project_root: path };
      sessions.getSessions().push(meta);
      sessions.setCurrentSessionId(id);
      return meta;
    },
  };
}

function response(data, status = 200) {
  return { ok: status >= 200 && status < 300, status,
    json: async () => data, text: async () => JSON.stringify(data) };
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
async function settle() { for (let i = 0; i < 12; i++) await Promise.resolve(); }

test('workspace saves through the current session and waits for the canonical response', async () => {
  const h = await harness();
  const meta = h.select('owner/session', '/projects/old');
  const reply = deferred();
  h.fetchWith(() => reply.promise);
  const saving = h.workspace.setWorkspace('/projects/link');
  await settle();
  assert.equal(h.workspace.getWorkspace(), '/projects/old');
  assert.equal(h.elements.get('workspace-indicator-name').textContent, 'old');
  assert.equal(h.calls.fetch.length, 1);
  const { url, options } = h.calls.fetch[0];
  assert.equal(url, 'https://assistant.example/api/session/owner%2Fsession');
  assert.equal(options.method, 'PATCH');
  assert.equal(options.credentials, 'same-origin');
  assert.ok(options.body instanceof URLSearchParams);
  assert.equal(options.body.get('project_root'), '/projects/link');
  let waitingDone = false;
  const waiting = h.workspace.waitForWorkspace().then(() => { waitingDone = true; });
  await settle();
  assert.equal(waitingDone, false);
  reply.resolve(response({ project_root: '/projects/resolved' }));
  await Promise.all([saving, waiting]);
  assert.equal(meta.project_root, '/projects/resolved');
  assert.equal(h.workspace.getWorkspace(), '/projects/resolved');
  assert.equal(h.elements.get('workspace-indicator-name').textContent, 'resolved');
});

test('a refused owner-backed picker save retains the old folder and open picker', async () => {
  const h = await harness();
  const meta = h.select('session-a', '/projects/old');
  h.fetchWith((url) => url.includes('/api/workspace/browse')
    ? response({ path: '/projects/rejected', parent: null, dirs: [] })
    : response({ detail: 'Workspace is not allowed' }, 403));
  await h.workspace.openWorkspaceBrowser();
  const button = h.elements.get('workspace-use');
  await button.dispatch('click');
  assert.equal(h.workspace.getWorkspace(), '/projects/old');
  assert.equal(meta.project_root, '/projects/old');
  assert.equal(h.elements.get('workspace-indicator-name').textContent, 'old');
  assert.equal(h.elements.get('workspace-modal').style.display, 'flex');
  assert.equal(button.disabled, false);
  assert.deepEqual(h.calls.errors, ['Workspace is not allowed']);
  assert.deepEqual(h.calls.toasts, []);
});

test('clear failure retains the selected folder; successful clear stores an empty root', async () => {
  const h = await harness();
  const meta = h.select('session-a', '/projects/old');
  h.fetchWith(() => response({ detail: 'Owner check failed' }, 403));
  assert.equal(await h.workspace.clearWorkspace(), false);
  assert.equal(h.workspace.getWorkspace(), '/projects/old');
  h.fetchWith(() => response({ project_root: '' }));
  assert.equal(await h.workspace.clearWorkspace(), true);
  assert.equal(h.calls.fetch[1].options.body.get('project_root'), '');
  assert.equal(meta.project_root, '');
  assert.equal(h.workspace.getWorkspace(), '');
  assert.equal(h.elements.get('workspace-indicator-btn').style.display, 'none');
});

test('switching sessions discards a global draft folder and restores each session root', async () => {
  const h = await harness();
  await h.workspace.setWorkspace('/projects/draft');
  assert.equal(h.Storage.get('fixture-workspace'), '/projects/draft');
  h.select('session-a', '/projects/a');
  assert.equal(h.Storage.get('fixture-workspace'), null);
  assert.equal(h.workspace.getWorkspace(), '/projects/a');
  h.select('session-b', '/projects/b');
  assert.equal(h.workspace.getWorkspace(), '/projects/b');
  h.sessions.setCurrentSessionId('session-a');
  assert.equal(h.workspace.getWorkspace(), '/projects/a');
  h.sessions.setCurrentSessionId(null);
  assert.equal(h.workspace.getWorkspace(), '');
  assert.equal(h.elements.get('workspace-indicator-btn').style.display, 'none');
  assert.equal(h.calls.fetch.length, 0);
});

test('a delayed save updates its owner metadata without replacing the current session root', async () => {
  const h = await harness();
  const a = h.select('session-a', '/projects/a');
  const reply = deferred();
  h.fetchWith(() => reply.promise);
  const saving = h.workspace.setWorkspace('/projects/a-new');
  await settle();
  const b = h.select('session-b', '/projects/b');
  reply.resolve(response({ project_root: '/projects/a-canonical' }));
  await saving;
  assert.equal(a.project_root, '/projects/a-canonical');
  assert.equal(b.project_root, '/projects/b');
  assert.equal(h.sessions.getCurrentSessionId(), 'session-b');
  assert.equal(h.workspace.getWorkspace(), '/projects/b');
  assert.equal(h.elements.get('workspace-indicator-name').textContent, 'b');
  h.sessions.setCurrentSessionId('session-a');
  assert.equal(h.workspace.getWorkspace(), '/projects/a-canonical');
});

test('another session save does not invalidate the current session pending history root', async () => {
  const h = await harness();
  h.select('session-b', '/projects/b');
  const a = h.select('session-a', '/projects/a');
  const patch = deferred();
  const history = deferred();
  h.fetchWith((url, options) => {
    if (options.method === 'PATCH') return patch.promise;
    if (url.endsWith('/api/history/session-b')) return history.promise;
    if (url.includes('/api/chat/stream_status/')) return response({}, 404);
    throw new Error('Unexpected fetch: ' + url);
  });
  const saving = h.workspace.setWorkspace('/projects/a-new');
  await settle();
  const selecting = h.sessions.selectSession('session-b');
  await settle();
  patch.resolve(response({ project_root: '/projects/a-new' }));
  await saving;
  history.resolve(response({ project_root: '/projects/b-saved', history: [] }));
  await selecting;
  assert.equal(a.project_root, '/projects/a-new');
  assert.equal(h.workspace.getWorkspace(), '/projects/b-saved');
  assert.equal(h.elements.get('workspace-indicator-name').textContent, 'b-saved');
});

test('queued choices are serialized and a failed choice does not prevent the next save', async () => {
  const h = await harness();
  h.select('session-a', '/projects/a');
  const first = deferred();
  h.fetchWith((_url, options) => options.body.get('project_root') === '/projects/first'
    ? first.promise : response({ project_root: '/projects/second' }));
  const one = h.workspace.setWorkspace('/projects/first');
  const rejected = assert.rejects(one, /Rejected first choice/);
  const two = h.workspace.setWorkspace('/projects/second');
  await settle();
  assert.equal(h.calls.fetch.length, 1);
  first.resolve(response({ detail: 'Rejected first choice' }, 422));
  await Promise.all([rejected, two, h.workspace.waitForWorkspace()]);
  assert.equal(h.calls.fetch.length, 2);
  assert.equal(h.workspace.getWorkspace(), '/projects/second');
});

test('out-of-order history responses cannot restore another session workspace', async () => {
  const h = await harness();
  h.select('session-a', '/projects/a');
  h.select('session-b', '/projects/b');
  const a = deferred();
  h.fetchWith((url) => {
    if (url.endsWith('/api/history/session-a')) return a.promise;
    if (url.endsWith('/api/history/session-b')) {
      return response({ project_root: '/projects/b-saved', history: [] });
    }
    if (url.includes('/api/chat/stream_status/')) return response({}, 404);
    throw new Error('Unexpected fetch: ' + url);
  });
  const selectingA = h.sessions.selectSession('session-a');
  await settle();
  assert.equal(h.workspace.getWorkspace(), '');
  await h.sessions.selectSession('session-b');
  a.resolve(response({ project_root: '/projects/a-stale', history: [] }));
  await selectingA;
  assert.equal(h.workspace.getWorkspace(), '/projects/b-saved');
  assert.equal(h.elements.get('workspace-indicator-name').textContent, 'b-saved');
  assert.deepEqual(h.calls.errors, []);
});

test('a delayed history response cannot replace a newer confirmed folder choice', async () => {
  const h = await harness();
  h.select('session-a', '/projects/old');
  const history = deferred();
  h.fetchWith((url, options) => {
    if (url.endsWith('/api/history/session-a')) return history.promise;
    if (options.method === 'PATCH') return response({ project_root: '/projects/new' });
    if (url.includes('/api/chat/stream_status/')) return response({}, 404);
    throw new Error('Unexpected fetch: ' + url);
  });
  const selecting = h.sessions.selectSession('session-a');
  await settle();
  await h.workspace.setWorkspace('/projects/new');
  history.resolve(response({ project_root: '/projects/old', history: [] }));
  await selecting;
  assert.equal(h.workspace.getWorkspace(), '/projects/new');
  assert.equal(h.elements.get('workspace-indicator-name').textContent, 'new');
  assert.deepEqual(h.calls.errors, []);
});

test('a refused folder choice does not suppress the authoritative history root', async () => {
  const h = await harness();
  h.select('session-a', '/projects/saved');
  const history = deferred();
  h.fetchWith((url, options) => {
    if (url.endsWith('/api/history/session-a')) return history.promise;
    if (options.method === 'PATCH') return response({ detail: 'Folder unavailable' }, 422);
    if (url.includes('/api/chat/stream_status/')) return response({}, 404);
    throw new Error('Unexpected fetch: ' + url);
  });
  const selecting = h.sessions.selectSession('session-a');
  await settle();
  await assert.rejects(h.workspace.setWorkspace('/projects/rejected'), /Folder unavailable/);
  history.resolve(response({ project_root: '/projects/saved', history: [] }));
  await selecting;
  assert.equal(h.workspace.getWorkspace(), '/projects/saved');
  assert.equal(h.elements.get('workspace-indicator-name').textContent, 'saved');
});

test('materializing a draft saves its folder on the new session without clearing the message', async () => {
  const h = await harness();
  h.sessions.createDirectChat('http://model.example/v1', 'local-model', 'endpoint-a');
  const composer = h.elements.get('message');
  composer.value = 'Fix the counter and run its tests';
  await h.workspace.setWorkspace('/projects/draft');
  h.fetchWith((url, options) => {
    if (url.endsWith('/api/session') && options.method === 'POST') return response({ id: 'created' });
    if (url.endsWith('/api/session/created')) return response({ project_root: '/projects/canonical' });
    if (url.endsWith('/api/sessions')) return response([{ id: 'created', name: 'New project', project_root: '/projects/canonical' }]);
    throw new Error('Unexpected fetch: ' + url);
  });
  assert.equal(await h.sessions.materializePendingSession(), true);
  assert.equal(h.sessions.getCurrentSessionId(), 'created');
  assert.equal(h.workspace.getWorkspace(), '/projects/canonical');
  assert.equal(h.Storage.get('fixture-workspace'), null);
  assert.equal(composer.value, 'Fix the counter and run its tests');
  assert.equal(h.calls.fetch[0].options.body.get('model'), 'local-model');
  assert.equal(h.calls.fetch[1].options.body.get('project_root'), '/projects/draft');
  assert.equal(h.calls.fetch[1].options.method, 'PATCH');
  assert.deepEqual(h.calls.errors, []);
});

test('a failed draft-folder save retains the draft and retries the same owned session', async () => {
  const h = await harness();
  h.sessions.createDirectChat('http://model.example/v1', 'local-model', 'endpoint-a');
  h.elements.get('message').value = 'Keep this unsent draft';
  await h.workspace.setWorkspace('/projects/draft');
  let patchAttempts = 0;
  h.fetchWith((url, options) => {
    if (options.method === 'POST') return response({ id: 'created' });
    if (options.method === 'PATCH') {
      patchAttempts++;
      return patchAttempts === 1 ? response({ detail: 'Folder unavailable' }, 422)
        : response({ project_root: '/projects/draft' });
    }
    if (url.endsWith('/api/sessions')) return response([{ id: 'created', name: 'Retried project', project_root: '/projects/draft' }]);
    throw new Error('Unexpected fetch: ' + url);
  });
  assert.equal(await h.sessions.materializePendingSession(), false);
  assert.equal(h.elements.get('message').value, 'Keep this unsent draft');
  assert.deepEqual(h.calls.errors, ['Folder unavailable']);
  assert.equal(h.calls.fetch.length, 2);
  assert.equal(h.sessions.getCurrentSessionId(), null);
  assert.equal(h.sessions.hasPendingChat(), true);
  assert.equal(h.workspace.getWorkspace(), '/projects/draft');
  assert.equal(await h.sessions.materializePendingSession(), true);
  assert.equal(h.sessions.getCurrentSessionId(), 'created');
  assert.equal(h.sessions.hasPendingChat(), false);
  assert.equal(h.workspace.getWorkspace(), '/projects/draft');
  assert.equal(h.elements.get('message').value, 'Keep this unsent draft');
  const creations = h.calls.fetch.filter(({ options }) => options.method === 'POST');
  const patches = h.calls.fetch.filter(({ options }) => options.method === 'PATCH');
  assert.equal(creations.length, 1);
  assert.equal(patches.length, 2);
  for (const patch of patches) {
    assert.equal(patch.url, 'https://assistant.example/api/session/created');
    assert.equal(patch.options.body.get('project_root'), '/projects/draft');
  }
});

test('Send waits for a pending folder save and preserves the unsent message when it is refused', async () => {
  const h = await harness({ realChat: true });
  h.select('session-a', '/projects/saved');
  h.elements.get('message').value = 'Do not lose this unsent task';
  // Repeat the refusal to prove the send-in-flight flag is released too.
  for (let attempt = 0; attempt < 2; attempt++) {
    const reply = deferred();
    h.fetchWith(() => reply.promise);
    const saving = h.workspace.setWorkspace('/projects/rejected');
    const refused = assert.rejects(saving, /Folder unavailable/);
    await settle();
    const sending = h.chat.handleChatSubmit({ preventDefault() {} });
    await settle();
    assert.equal(h.elements.get('message').value, 'Do not lose this unsent task');
    assert.equal(h.elements.get('message').disabled, true);
    assert.equal(h.elements.get('send-btn').classList.contains('send-pending'), true);
    assert.deepEqual(h.calls.rendered, []);
    reply.resolve(response({ detail: 'Folder unavailable' }, 422));
    await Promise.all([refused, sending]);
    assert.equal(h.elements.get('message').value, 'Do not lose this unsent task');
    assert.equal(h.elements.get('message').disabled, false);
    assert.equal(h.elements.get('send-btn').classList.contains('send-pending'), false);
    assert.equal(h.workspace.getWorkspace(), '/projects/saved');
  }
  assert.deepEqual(h.calls.errors, ['Folder unavailable', 'Folder unavailable']);
  assert.equal(h.calls.fetch.length, 2);
  assert.ok(h.calls.fetch.every(({ options }) => options.method === 'PATCH'));
  assert.deepEqual(h.calls.rendered, []);
  assert.deepEqual(h.calls.consoleErrors, []);
});

test('Send does not consume or route a draft after the conversation changes during a folder save', async () => {
  const h = await harness({ realChat: true });
  h.select('session-a', '/projects/a');
  h.elements.get('message').value = 'Review this task before sending elsewhere';
  const reply = deferred();
  h.fetchWith(() => reply.promise);
  const saving = h.workspace.setWorkspace('/projects/a-new');
  await settle();
  const sending = h.chat.handleChatSubmit({ preventDefault() {} });
  await settle();
  h.select('session-b', '/projects/b');
  reply.resolve(response({ project_root: '/projects/a-new' }));
  await Promise.all([saving, sending]);
  assert.equal(h.elements.get('message').value, 'Review this task before sending elsewhere');
  assert.equal(h.elements.get('message').disabled, false);
  assert.equal(h.sessions.getCurrentSessionId(), 'session-b');
  assert.equal(h.workspace.getWorkspace(), '/projects/b');
  assert.deepEqual(h.calls.errors, ['Conversation changed. Review your message before sending.']);
  assert.equal(h.calls.fetch.length, 1);
  assert.deepEqual(h.calls.rendered, []);
  assert.deepEqual(h.calls.consoleErrors, []);
});

for (const [shape, diff, expectedMarkup] of [
  ['structured edit', { file: 'summary.mjs', text: '--- a/summary.mjs\n+++ b/summary.mjs\n@@ -1 +1 @@\n-return 0;\n+return 1;', added: 1, removed: 1 }, 'agent-tool-diff'],
  ['legacy text', '--- a/summary.mjs\n+++ b/summary.mjs\n@@ -1 +1 @@\n-return 0;\n+return 1;', 'agent-diff'],
]) {
  test(`saved ${shape} tool diff restores every round and the final assistant result`, async () => {
    const h = await harness({ realRenderer: true });
    const result = h.renderer.addMessage('assistant', 'All three tests passed.', 'local-model', {
      round_texts: ['Inspect the counter.', 'Fix the count.', 'Run the tests.', 'All three tests passed.'],
      tool_events: [
        { round: 1, tool: 'read_file', command: 'summary.mjs', output: 'return 0;', exit_code: 0 },
        { round: 2, tool: 'edit_file', command: 'summary.mjs', diff, exit_code: 0 },
        { round: 3, tool: 'execute', command: 'npm test', output: '3 passed', exit_code: 0 },
      ],
    });
    assert.deepEqual(h.calls.consoleErrors, []);
    const history = h.elements.get('chat-history');
    const messages = history.querySelectorAll('.msg-ai');
    const tools = history.querySelectorAll('.agent-thread-node');
    assert.equal(messages.length, 4);
    assert.equal(tools.length, 3);
    assert.equal(result, messages[3]);
    assert.equal(result.dataset.raw, 'All three tests passed.');
    assert.ok(tools[1].innerHTML.includes(expectedMarkup));
    assert.ok(tools[1].innerHTML.includes('summary.mjs'));
    assert.ok(tools[2].innerHTML.includes('npm test'));
    assert.ok(tools[2].innerHTML.includes('3 passed'));
  });
}

async function connectedTerminal(h) {
  await h.elements.get('tool-terminal-btn').dispatch('click');
  assert.equal(h.sockets.length, 1);
  h.sockets[0].open();
  return { socket: h.sockets[0], term: h.terminals[0] };
}

test('ordinary Terminal hide and reopen preserve the socket and shell input', async () => {
  const h = await harness();
  h.select('session-a', '/projects/a');
  const { socket, term } = await connectedTerminal(h);
  assert.equal(socket.url, 'wss://assistant.example/ws/terminal?session_id=session-a');
  term.data('pwd\r');
  await h.elements.get('terminal-close').dispatch('click');
  assert.equal(h.elements.get('terminal-overlay').style.display, 'none');
  await h.elements.get('tool-terminal-btn').dispatch('click');
  term.data('ls\r');
  assert.equal(h.sockets.length, 1);
  assert.equal(socket.closeCount, 0);
  assert.deepEqual(socket.input(), [{ type: 'input', data: 'pwd\r' }, { type: 'input', data: 'ls\r' }]);
  assert.equal(h.elements.get('terminal-connect-btn').style.display, 'none');
});

test('a saved project change blocks old-shell input until explicit Terminal reconnect', async () => {
  const h = await harness();
  h.select('session-a', '/projects/a');
  const { socket, term } = await connectedTerminal(h);
  h.fetchWith(() => response({ project_root: '/projects/new' }));
  await h.workspace.setWorkspace('/projects/new');
  assert.equal(h.elements.get('terminal-connect-btn').textContent, 'Reconnect');
  assert.equal(h.elements.get('terminal-connect-btn').style.display, '');
  assert.match(h.elements.get('terminal-status').textContent, /reconnect/i);
  term.data('dangerous old cwd\r');
  await h.elements.get('terminal-close').dispatch('click');
  await h.elements.get('tool-terminal-btn').dispatch('click');
  assert.equal(h.sockets.length, 1);
  assert.equal(socket.closeCount, 0);
  assert.deepEqual(socket.input(), []);
  await h.elements.get('terminal-connect-btn').dispatch('click');
  assert.equal(socket.closeCount, 1);
  assert.equal(h.sockets.length, 2);
  h.sockets[1].open();
  term.data('pwd\r');
  assert.deepEqual(h.sockets[1].input(), [{ type: 'input', data: 'pwd\r' }]);
  assert.equal(h.elements.get('terminal-connect-btn').style.display, 'none');
});

test('switching sessions requires reconnect to the new session even when roots match', async () => {
  const h = await harness();
  h.select('session-a', '/projects/shared');
  const { socket, term } = await connectedTerminal(h);
  h.select('session/b', '/projects/shared');
  term.data('old session input\r');
  assert.deepEqual(socket.input(), []);
  assert.equal(h.elements.get('terminal-connect-btn').textContent, 'Reconnect');
  await h.elements.get('terminal-connect-btn').dispatch('click');
  assert.equal(h.sockets[1].url, 'wss://assistant.example/ws/terminal?session_id=session%2Fb');
  h.sockets[1].open();
  term.data('new session input\r');
  assert.deepEqual(h.sockets[1].input(), [{ type: 'input', data: 'new session input\r' }]);
});

test('returning to the still-bound project restores Connected without replacing the shell', async () => {
  const h = await harness();
  h.select('session-a', '/projects/a');
  const { socket, term } = await connectedTerminal(h);
  h.select('session-b', '/projects/b');
  assert.equal(h.elements.get('terminal-connect-btn').textContent, 'Reconnect');
  h.sessions.setCurrentSessionId('session-a');
  assert.equal(h.elements.get('terminal-status').textContent, 'Connected');
  assert.equal(h.elements.get('terminal-connect-btn').style.display, 'none');
  term.data('original shell\r');
  assert.deepEqual(socket.input(), [{ type: 'input', data: 'original shell\r' }]);
  assert.equal(h.sockets.length, 1);
  assert.equal(socket.closeCount, 0);
});
