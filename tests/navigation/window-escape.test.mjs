import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

// Run the complete application modules, without source extraction or rewrites.
// Only browser APIs and unrelated imported services are synthetic. Geometry and
// real pointer hit-testing remain the running-app browser replay's responsibility.
async function harness(width = 1440) {
  const observers = [];
  const listeners = new Map();
  const calls = { closed: [], chatAborts: 0, localEscape: 0, documentEscape: 0 };
  let document;
  function notify(target, type = 'attributes', attributeName = 'style', addedNodes = [], oldValue = null) {
    for (const observer of observers) {
      if (!observer.target || !document.contains(target)) continue;
      const options = observer.options;
      if (type === 'attributes' && (!options.attributes ||
          (options.attributeFilter && !options.attributeFilter.includes(attributeName)))) continue;
      if (type === 'childList' && !options.childList) continue;
      observer.pending.push({ target, type, attributeName, addedNodes,
        oldValue: options.attributeOldValue ? oldValue : null });
    }
  }
  class Element {
    constructor(tag = 'div', id = '', classes = '') {
      this.nodeType = 1;
      this.tagName = tag.toUpperCase();
      this.id = id;
      this.children = [];
      this.parentElement = null;
      this.dataset = {};
      this.attributes = {};
      this.isContentEditable = false;
      this.events = new Map();
      const names = new Set(classes.split(/\s+/).filter(Boolean));
      this.classNameValue = () => [...names].join(' ');
      const changeClasses = (change) => {
        const oldValue = this.classNameValue();
        change();
        notify(this, 'attributes', 'class', [], oldValue);
      };
      this.classList = {
        contains: (name) => names.has(name),
        add: (...values) => changeClasses(() => values.forEach((name) => names.add(name))),
        remove: (...values) => changeClasses(() => values.forEach((name) => names.delete(name))),
        toggle: (name, force = !names.has(name)) => {
          changeClasses(() => force ? names.add(name) : names.delete(name));
          return force;
        },
      };
      const styleValues = { display: '', zIndex: '' };
      this.styleText = () => Object.entries(styleValues)
        .filter(([, value]) => typeof value === 'string' && value !== '')
        .map(([key, value]) => `${key.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase())}: ${value};`)
        .join(' ') || null;
      this.style = new Proxy(styleValues, {
        set: (style, key, value) => {
          // Browser replay confirmed that assigning display='' again to an
          // already-visible tool does not deliver another style mutation.
          if (style[key] === value) return true;
          const oldValue = this.styleText();
          style[key] = value;
          notify(this, 'attributes', 'style', [], oldValue);
          return true;
        },
      });
      this.style.setProperty = (key, value) => { this.style[key === 'z-index' ? 'zIndex' : key] = value; };
    }
    appendChild(child) {
      child.parentElement = this;
      this.children.push(child);
      notify(this, 'childList', null, [child]);
      return child;
    }
    contains(node) { return node === this || this.children.some((child) => child.contains(node)); }
    matches(selector) {
      return selector.split(',').some((part) => {
        const tokens = part.trim().split(/\s+/);
        const own = tokens.pop();
        const matchesSimple = (node, value) => {
          if (!node) return false;
          for (const [, excluded] of value.matchAll(/:not\(([^)]+)\)/g)) {
            if (matchesSimple(node, excluded)) return false;
          }
          value = value.replace(/:not\([^)]+\)/g, '');
          const tag = value.match(/^[a-z]+/i)?.[0];
          if (tag && node.tagName.toLowerCase() !== tag.toLowerCase()) return false;
          const id = value.match(/#([\w-]+)/)?.[1];
          if (id && node.id !== id) return false;
          if ([...value.matchAll(/\.([\w-]+)/g)].some(([, name]) => !node.classList.contains(name))) return false;
          for (const [, name, op, wanted] of value.matchAll(/\[([\w-]+)(?:(\$?=)["']?([^\]"']*)["']?)?\]/g)) {
            const actual = name === 'id' ? node.id : node.attributes[name];
            if (actual == null || (op === '=' && actual !== wanted) ||
                (op === '$=' && !actual.endsWith(wanted))) return false;
          }
          return true;
        };
        if (!matchesSimple(this, own)) return false;
        let parent = this.parentElement;
        while (tokens.length) {
          const wanted = tokens.pop();
          while (parent && !matchesSimple(parent, wanted)) parent = parent.parentElement;
          if (!parent) return false;
          parent = parent.parentElement;
        }
        return true;
      });
    }
    closest(selector) {
      for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node;
      return null;
    }
    querySelectorAll(selector) {
      return this.children.flatMap((child) => [
        ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector),
      ]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    getAttribute(name) {
      if (name === 'style') return this.styleText();
      if (name === 'class') return this.classNameValue();
      return this.attributes[name] ?? null;
    }
    addEventListener(type, handler, options) {
      if (!this.events.has(type)) this.events.set(type, []);
      this.events.get(type).push({ handler, capture: options === true || options?.capture === true });
    }
    click() { this.onClick?.(); }
    getBoundingClientRect() { return { left: 0, top: 0, right: width, bottom: 844, width, height: 844 }; }
    getClientRects() { return [this.getBoundingClientRect()]; }
  }
  const body = new Element('body');
  document = {
    body, readyState: 'loading',
    contains: (node) => body.contains(node),
    querySelectorAll: (selector) => body.querySelectorAll(selector),
    querySelector: (selector) => body.querySelector(selector),
    getElementById: (id) => body.querySelector('#' + id),
    elementFromPoint: () => null,
    addEventListener(type, handler, options) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push({ handler, capture: options === true || options?.capture === true });
    },
    removeEventListener() {},
  };
  class MutationObserver {
    constructor(callback) { this.callback = callback; this.pending = []; observers.push(this); }
    observe(target, options) { this.target = target; this.options = options; }
  }
  function flush() {
    for (let turn = 0; turn < 100; turn++) {
      const batches = observers.map((observer) => [observer, observer.pending.splice(0)]);
      if (batches.every(([, mutations]) => !mutations.length)) return;
      for (const [observer, mutations] of batches) if (mutations.length) observer.callback(mutations);
    }
    throw new Error('window promotion observer did not settle');
  }
  function dispatch(type, target = body, extra = {}) {
    const event = {
      type, target, key: 'Escape', code: 'Escape', defaultPrevented: false,
      ctrlKey: false, altKey: false, shiftKey: false, metaKey: false,
      stopped: false, immediate: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { this.stopped = true; },
      stopImmediatePropagation() { this.stopped = true; this.immediate = true; },
      ...extra,
    };
    const run = (entries, capture) => {
      for (const entry of entries || []) {
        if (event.immediate) break;
        if (entry.capture === capture) entry.handler(event);
      }
    };
    const path = [];
    for (let node = target; node; node = node.parentElement) path.push(node);
    run(listeners.get(type), true);
    for (const node of [...path].reverse()) {
      if (event.stopped) break;
      run(node.events.get(type), true);
    }
    for (const node of path) {
      if (event.stopped) break;
      run(node.events.get(type), false);
    }
    if (!event.stopped) run(listeners.get(type), false);
    return event;
  }
  const context = vm.createContext({
    document, MutationObserver, console,
    window: { innerWidth: width, innerHeight: 844, addEventListener() {} },
    navigator: { platform: 'Linux', userAgent: 'synthetic regression' },
    getComputedStyle: (node) => ({
      display: node.classList.contains('hidden') ? 'none' : node.style.display || 'flex',
      visibility: 'visible', opacity: '1', zIndex: node.style.zIndex || '250',
    }),
    setTimeout: () => 1, clearTimeout() {}, requestAnimationFrame: () => 1,
    fetch: async () => ({ json: async () => ({}) }),
  });
  const modules = new Map();
  async function load(url) {
    const id = url.href;
    if (modules.has(id)) return modules.get(id);
    let module;
    if (/\/(theme|spinner|modalManager)\.js$/.test(url.pathname)) {
      module = new vm.SyntheticModule(['default', 'isRegistered', 'close', 'restore', 'minimize'], function () {
        this.setExport('default', {});
        this.setExport('isRegistered', () => false);
        for (const name of ['close', 'restore', 'minimize']) this.setExport(name, () => {});
      }, { context, identifier: id });
    } else {
      module = new vm.SourceTextModule(readFileSync(url, 'utf8'), { context, identifier: id });
    }
    modules.set(id, module);
    await module.link((specifier, owner) => load(new URL(specifier, owner.identifier)));
    return module;
  }
  const base = new URL('../../static/js/', import.meta.url);
  const ui = await load(new URL('ui.js', base));
  await ui.evaluate();
  const keyboard = await load(new URL('keyboard-shortcuts.js', base));
  await keyboard.evaluate();
  keyboard.namespace.initKeyboardShortcuts({
    el: document.getElementById, uiModule: ui.namespace.default,
    chatModule: { abortCurrentRequest: () => { calls.chatAborts++; } },
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') calls.documentEscape++;
  });
  const menus = await load(new URL('escMenuStack.js', base));
  function makeWindow(id, coding = false) {
    const node = new Element('div', id, coding ? 'crew-overlay tool-overlay' : 'modal');
    node.style.display = 'none';
    const content = node.appendChild(new Element('div', '', coding ? 'crew-modal' : 'modal-content'));
    const close = content.appendChild(new Element('button', '', coding ? 'crew-close' : 'close-btn'));
    close.onClick = () => { calls.closed.push(id); node.style.display = 'none'; };
    body.appendChild(node);
    flush();
    return { node, content, close };
  }
  function show(win, settle = true) { win.node.style.display = ''; if (settle) flush(); }
  function input(win, tag = 'textarea') {
    const node = win.content.appendChild(new Element(tag));
    node.addEventListener('keydown', () => { calls.localEscape++; });
    return node;
  }
  return { calls, body, Element, makeWindow, show, input, dispatch, flush,
    ui: ui.namespace, menus: menus.namespace };
}

for (const width of [1440, 390]) {
  for (const order of [['terminal-overlay', 'preview-overlay'], ['preview-overlay', 'terminal-overlay']]) {
    test(`Escape closes only the most recent tool via its owner at ${width}: ${order.join(' then ')}`, async () => {
      const h = await harness(width);
      const lower = h.makeWindow('ordinary-modal');
      const first = h.makeWindow(order[0], true);
      const second = h.makeWindow(order[1], true);
      h.show(lower); h.show(first); h.show(second);
      const event = h.dispatch('keydown');
      assert.deepEqual(h.calls.closed, [order[1]]);
      assert.equal(lower.node.style.display, '');
      assert.equal(first.node.style.display, '');
      assert.equal(event.defaultPrevented, true);
      assert.equal(event.immediate, true);
      assert.equal(h.calls.chatAborts, 0);
      h.flush();
      h.show(second);
      assert.equal(h.ui.getTopWindow().id, order[1]);
    });
  }
}

test('a later ordinary modal owns Escape above the coding tool', async () => {
  const h = await harness();
  const tool = h.makeWindow('terminal-overlay', true);
  const ordinary = h.makeWindow('ordinary-modal');
  h.show(tool); h.show(ordinary);
  h.dispatch('keydown');
  assert.deepEqual(h.calls.closed, ['ordinary-modal']);
  assert.equal(tool.node.style.display, '');
  assert.equal(h.calls.chatAborts, 0);
});

test('coalesced opens and refit mutations settle without promoting each other forever', async () => {
  const h = await harness();
  const terminal = h.makeWindow('terminal-overlay', true);
  const preview = h.makeWindow('preview-overlay', true);
  h.show(terminal, false);
  h.show(preview, false);
  h.flush();
  assert.equal(h.ui.getTopWindow().id, 'preview-overlay');
  terminal.node.style.width = '700px';
  preview.node.style.height = '600px';
  h.flush();
  assert.equal(h.ui.getTopWindow().id, 'preview-overlay');
  h.dispatch('keydown');
  assert.deepEqual(h.calls.closed, ['preview-overlay']);
});

test('reopening an already-visible lower tool promotes it above another open window', async () => {
  const h = await harness();
  const terminal = h.makeWindow('terminal-overlay', true);
  const ordinary = h.makeWindow('ordinary-modal');
  h.show(terminal);
  h.show(ordinary);
  assert.equal(h.ui.getTopWindow().id, 'ordinary-modal');
  h.show(terminal);
  assert.equal(h.ui.getTopWindow().id, 'ordinary-modal');
  h.ui.focusWindow(terminal.node);
  h.flush();
  assert.equal(h.ui.getTopWindow().id, 'terminal-overlay');
  h.dispatch('keydown');
  assert.deepEqual(h.calls.closed, ['terminal-overlay']);
  assert.equal(ordinary.node.style.display, '');
});

test('a tool covering a remembered hovered window protects that lower window and chat expansion', async () => {
  const h = await harness();
  const lower = h.makeWindow('ordinary-modal');
  const tool = h.makeWindow('preview-overlay', true);
  const thinking = h.body.appendChild(new h.Element('div', '', 'thinking-content expanded'));
  h.show(lower);
  h.dispatch('pointerover', lower.content, { clientX: 40, clientY: 40 });
  h.show(tool);
  h.dispatch('keydown');
  assert.deepEqual(h.calls.closed, ['preview-overlay']);
  assert.equal(thinking.classList.contains('expanded'), true);
  assert.equal(lower.node.style.display, '');
});

test('menus above a coding tool retain one-per-Escape LIFO dismissal, including an input target', async () => {
  const h = await harness();
  const tool = h.makeWindow('preview-overlay', true);
  h.show(tool);
  const input = h.input(tool, 'input');
  const order = [];
  h.menus.registerMenuDismiss(() => order.push('older'));
  h.menus.registerMenuDismiss(() => order.push('newer'));
  h.dispatch('keydown', input);
  assert.deepEqual(order, ['newer']);
  assert.deepEqual(h.calls.closed, []);
  assert.equal(h.calls.localEscape, 0);
  h.dispatch('keydown');
  assert.deepEqual(order, ['newer', 'older']);
  assert.deepEqual(h.calls.closed, []);
  h.dispatch('keydown');
  assert.deepEqual(h.calls.closed, ['preview-overlay']);
});

for (const [id, tag] of [['terminal-overlay', 'textarea'], ['preview-overlay', 'input'], ['preview-overlay', 'select']]) {
  test(`Escape remains with the ${id} ${tag} and cannot abort background chat`, async () => {
    const h = await harness();
    const tool = h.makeWindow(id, true);
    h.show(tool);
    const event = h.dispatch('keydown', h.input(tool, tag));
    assert.deepEqual(h.calls.closed, []);
    assert.equal(h.calls.localEscape, 1);
    assert.equal(h.calls.chatAborts, 0);
    assert.equal(h.calls.documentEscape, 0);
    assert.equal(event.stopped, true);
    assert.equal(event.defaultPrevented, false);
  });
}

test('ordinary modal input Escape still reaches the input without dismissing its window', async () => {
  const h = await harness();
  const ordinary = h.makeWindow('ordinary-modal');
  h.show(ordinary);
  const event = h.dispatch('keydown', h.input(ordinary));
  assert.deepEqual(h.calls.closed, []);
  assert.equal(h.calls.localEscape, 1);
  assert.equal(h.calls.documentEscape, 1);
  assert.equal(event.defaultPrevented, false);
});

test('a covered bulk-cancel bar cannot steal tool input Escape but still owns ordinary selection cancellation', async () => {
  const h = await harness();
  const ordinary = h.makeWindow('ordinary-modal');
  const tool = h.makeWindow('terminal-overlay', true);
  const cancel = ordinary.content.appendChild(new h.Element('button', 'library-bulk-cancel'));
  let cancellations = 0;
  cancel.onClick = () => { cancellations++; };
  h.show(ordinary);
  h.show(tool);
  h.dispatch('keydown', h.input(tool));
  assert.equal(cancellations, 0);
  assert.equal(h.calls.localEscape, 1);
  assert.equal(h.calls.documentEscape, 0);
  assert.equal(h.calls.chatAborts, 0);
  tool.close.click();
  h.flush();
  const event = h.dispatch('keydown', h.input(ordinary));
  assert.equal(cancellations, 1);
  assert.equal(h.calls.localEscape, 1);
  assert.equal(ordinary.node.style.display, '');
  assert.equal(event.defaultPrevented, true);
});

test('minimized and display-hidden tools cannot own Escape or suppress ordinary chat cancel', async () => {
  const h = await harness();
  const minimized = h.makeWindow('terminal-overlay', true);
  h.show(minimized);
  minimized.node.classList.add('modal-minimized');
  h.flush();
  h.makeWindow('preview-overlay', true);
  h.dispatch('keydown');
  assert.deepEqual(h.calls.closed, []);
  assert.equal(h.calls.chatAborts, 1);
  assert.equal(h.calls.documentEscape, 1);
});

test('the arbiter leaves an already-prevented Escape untouched', async () => {
  const h = await harness();
  const tool = h.makeWindow('preview-overlay', true);
  h.show(tool);
  const event = h.dispatch('keydown', h.body, { defaultPrevented: true });
  assert.deepEqual(h.calls.closed, []);
  assert.equal(h.calls.chatAborts, 0);
  assert.equal(event.immediate, false);
});
