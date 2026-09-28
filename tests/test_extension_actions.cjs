const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

const PAGE_URL = 'https://example.com/';
const PAGE_KEY = [123, PAGE_URL];

function createBridge(options = {}) {
  let actionCalls = 0;
  let watchCalls = 0;
  let clicks = 0;
  const updateCalls = [];
  const listeners = new Set();
  const location = {href: PAGE_URL};
  const body = {innerText: options.initialText || 'Before'};
  let hitElement;
  let busy = false;
  const busyNode = {
    checkVisibility() { return true; },
    getBoundingClientRect() {
      return {x: 0, y: 0, top: 0, left: 0, width: 20, height: 20, bottom: 20, right: 20};
    },
  };
  const document = {
    body,
    title: 'Example',
    querySelectorAll(selector) {
      return selector === '[aria-busy="true"],[role="progressbar"]' && busy ? [busyNode] : [];
    },
    elementFromPoint() { return hitElement; },
  };
  let guard = [7, options.role || 'button', 'Continue', null, null, null, null,
    false, null, null, null, null, options.href || null, 'Continue'];
  const element = {
    isConnected: true,
    tagName: options.role === 'link' ? 'A' : 'BUTTON',
    type: 'button',
    readOnly: false,
    value: '',
    checked: false,
    selectedIndex: -1,
    matches() { return false; },
    closest() { return null; },
    checkVisibility() { return true; },
    getBoundingClientRect() { return {x: 10, y: 10, width: 80, height: 24, bottom: 34, right: 90}; },
    contains() { return true; },
    getAttribute(name) { return name === 'href' ? (options.href || null) : null; },
    focus() { options.onFocus?.(); },
    click() { clicks += 1; options.onClick?.(); },
  };
  hitElement = element;
  const window = {
    __jevFast: {
      nodes: new Map([[7, element]]),
      pageKey: () => options.stale ? ['changed'] : PAGE_KEY,
      guard: () => guard,
    },
    scrollBy() {},
  };
  const tab = {
    id: 7, url: PAGE_URL, title: 'Example', active: false, windowId: 1, status: 'complete',
  };

  const emitUpdate = changeInfo => {
    for (const listener of [...listeners]) listener(tab.id, changeInfo, {...tab});
  };
  const context = vm.createContext({
    URL,
    window,
    location,
    document,
    innerWidth: 1024,
    innerHeight: 768,
    scrollX: 0,
    scrollY: 0,
    importScripts() {},
    setInterval() {},
    setTimeout,
    clearTimeout,
    self: {},
    chrome: {
      tabs: {
        async get(id) {
          assert.equal(id, tab.id);
          return {...tab};
        },
        async update(id, values) {
          updateCalls.push({...values});
          Object.assign(tab, values);
          return {...tab};
        },
        async captureVisibleTab() { return 'data:image/png;base64,AA=='; },
        onUpdated: {
          addListener(listener) { listeners.add(listener); },
          removeListener(listener) { listeners.delete(listener); },
        },
      },
      scripting: {
        async executeScript(details) {
          if (details.func?.name === 'pageAct') actionCalls += 1;
          if (details.func?.name === 'readActionWatch') {
            watchCalls += 1;
            if (options.readError) throw new Error('post-action observation failed');
          }
          return [{result: details.func(...details.args)}];
        },
      },
      alarms: {create() {}, onAlarm: {addListener() {}}},
      runtime: {
        id: 'test-extension',
        getManifest: () => ({version: '1.0.4'}),
        onInstalled: {addListener() {}},
        onStartup: {addListener() {}},
      },
    },
  });
  vm.runInContext(readFileSync(join(__dirname, '../extension/background.js'), 'utf8'), context);

  const action = {id: 'e1', kind: 'click', node: 7, role: options.role || 'button'};
  const params = {tabId: '7', action, pageKey: PAGE_KEY, guard, url: PAGE_URL};
  return {
    context,
    tab,
    location,
    body,
    action,
    params,
    updateCalls,
    get actionCalls() { return actionCalls; },
    get watchCalls() { return watchCalls; },
    get clicks() { return clicks; },
    emitUpdate,
    observeTarget(node, label) {
      hitElement = {...element};
      window.__jevFast.nodes.set(node, hitElement);
      guard = [node, 'button', label, ...guard.slice(3)];
      params.guard = [...guard];
      params.action = {...action, id: 'e2', node};
    },
    setBusy(value) { busy = value; },
    execute: () => context.execute({method: 'act', params}),
  };
}

test('an immediate DOM click settles without busy semantics in under 700ms', async t => {
  const bridge = createBridge({onClick() { bridge.body.innerText = 'SUCCESS'; }});
  const started = performance.now();
  const result = await bridge.execute();
  const elapsed = performance.now() - started;
  t.diagnostic('Immediate DOM click: ' + elapsed.toFixed(1) + 'ms');
  assert.ok(elapsed < 700, 'Immediate click took ' + elapsed.toFixed(1) + 'ms');
  assert.deepEqual({...result}, {executed: 'e1', settled: true, settlement: 'visible-change'});
  assert.equal(bridge.clicks, 1);
  assert.equal(bridge.actionCalls, 1);
});

test('a menu can open and its next observed target can execute without busy semantics', async t => {
  let menuOpen = false;
  const bridge = createBridge({onClick() {
    bridge.body.innerText = menuOpen ? 'Item selected' : 'Menu: choose an item';
    menuOpen = true;
  }});
  const started = performance.now();
  const opened = await bridge.execute();
  assert.equal(opened.settled, true, 'Opening the menu must allow the next action');
  assert.equal(bridge.body.innerText, 'Menu: choose an item');
  bridge.observeTarget(8, 'Choose item');
  const selected = await bridge.execute();
  const elapsed = performance.now() - started;
  t.diagnostic('Two menu actions: ' + elapsed.toFixed(1) + 'ms');
  assert.ok(elapsed < 700, 'Two menu actions took ' + elapsed.toFixed(1) + 'ms');
  assert.deepEqual({...selected}, {executed: 'e2', settled: true, settlement: 'visible-change'});
  assert.equal(bridge.body.innerText, 'Item selected');
  assert.equal(bridge.actionCalls, 2);
  assert.equal(bridge.clicks, 2);
  assert.deepEqual(bridge.updateCalls, []);
});

test('waits for a delayed button navigation and for pendingUrl to clear', async () => {
  const destination = 'https://example.com/details';
  const bridge = createBridge({onClick() {
    setTimeout(() => {
      bridge.tab.status = 'loading';
      bridge.tab.pendingUrl = destination;
      bridge.emitUpdate({status: 'loading', url: destination});
    }, 1500);
    setTimeout(() => {
      bridge.tab.status = 'complete';
      bridge.tab.url = destination;
      bridge.tab.pendingUrl = destination;
      bridge.emitUpdate({status: 'complete'});
    }, 1580);
    setTimeout(() => {
      delete bridge.tab.pendingUrl;
      bridge.location.href = destination;
      bridge.body.innerText = 'Details';
      bridge.emitUpdate({});
    }, 1670);
  }});

  const started = Date.now();
  const result = await bridge.execute();
  assert.ok(Date.now() - started >= 1500);
  assert.deepEqual({...result}, {
    executed: 'e1', settled: true, settlement: 'navigation-complete', url: destination,
  });
  assert.equal(bridge.actionCalls, 1);
  assert.deepEqual(bridge.updateCalls, []);
  assert.equal(bridge.tab.active, false);
});

for (const delayMs of [600, 1500]) {
test('waits for semantic aria-busy to clear after ' + delayMs + 'ms', async () => {
  const bridge = createBridge({onClick() {
    bridge.body.innerText = 'Loading...';
    bridge.setBusy(true);
    setTimeout(() => {
      bridge.body.innerText = 'Details are ready';
      bridge.setBusy(false);
    }, delayMs);
  }});

  const started = Date.now();
  const result = await bridge.execute();
  assert.ok(Date.now() - started >= delayMs);
  assert.deepEqual({...result}, {executed: 'e1', settled: true, settlement: 'visible-change'});
  assert.ok(bridge.watchCalls > 1);
  assert.equal(bridge.actionCalls, 1);
  assert.equal(bridge.clicks, 1);
});
}

test('post-action observation failure returns an inconclusive execution receipt', async () => {
  const bridge = createBridge({readError: true});
  const result = await bridge.execute();

  assert.deepEqual({...result}, {
    executed: 'e1', settled: false, settlement: 'observation-unavailable',
  });
  assert.equal(bridge.actionCalls, 1);
  assert.doesNotMatch(JSON.stringify(result), /STALE_PAGE/);
});

test('a stale pre-action snapshot is rejected before clicking', async () => {
  const bridge = createBridge({stale: true});
  await assert.rejects(bridge.execute(), /STALE_PAGE/);
  assert.equal(bridge.actionCalls, 1);
  assert.equal(bridge.clicks, 0);
});

test('focus alone does not change the visible-state fingerprint', () => {
  const bridge = createBridge({onFocus() { bridge.windowFocused = true; }});
  const result = bridge.context.pageAct(
    bridge.action, PAGE_KEY, bridge.params.guard, PAGE_URL, null, 'focus-check'
  );
  assert.equal(result.baseline, result.immediate);
});

test('ordinary article text does not signal pending work', () => {
  const bridge = createBridge({initialText: 'This article covers loading files; please wait for the next section.'});
  const result = bridge.context.pageAct(
    bridge.action, PAGE_KEY, bridge.params.guard, PAGE_URL, null, 'article-text'
  );
  assert.equal(result.immediateBusy, false);
});

test('screenshot activation remains explicit', async () => {
  const bridge = createBridge();
  const result = await bridge.context.execute({method: 'capture', params: {tabId: '7'}});
  assert.deepEqual({...result}, {data: 'AA=='});
  assert.deepEqual(bridge.updateCalls, [{active: true}]);
});

test('old WebSocket callbacks cannot affect or answer through its replacement', async () => {
  const connections = [];
  class FakeWebSocket {
    static OPEN = 1;
    static CONNECTING = 0;
    constructor(url) {
      this.url = url;
      this.readyState = FakeWebSocket.CONNECTING;
      this.sent = [];
      this.closeCalls = 0;
      connections.push(this);
    }
    send(value) { this.sent.push(JSON.parse(value)); }
    close() { this.closeCalls += 1; this.readyState = 3; }
  }
  const listener = {addListener() {}};
  const context = vm.createContext({
    URL, WebSocket: FakeWebSocket, importScripts() {}, setInterval() {}, setTimeout, clearTimeout,
    self: {JEV_BRIDGE_CONFIG: {port: 8765, token: 'local-test-token'}},
    chrome: {
      alarms: {create() {}, onAlarm: listener},
      runtime: {
        id: 'test-extension', getManifest: () => ({version: '1.0.4'}),
        onInstalled: listener, onStartup: listener,
      },
    },
  });
  vm.runInContext(readFileSync(join(__dirname, '../extension/background.js'), 'utf8'), context);
  const oldConnection = connections[0];
  oldConnection.readyState = FakeWebSocket.OPEN;
  oldConnection.onopen();

  const pendingReply = oldConnection.onmessage({data: JSON.stringify({
    type: 'command', id: 'old-session', method: 'status', params: {},
  })});
  oldConnection.readyState = 3;
  context.connect();
  const replacement = connections[1];
  replacement.readyState = FakeWebSocket.OPEN;
  replacement.onopen();
  await pendingReply;

  oldConnection.onerror();
  oldConnection.onclose();
  context.connect();

  assert.equal(replacement.closeCalls, 0);
  assert.equal(replacement.readyState, FakeWebSocket.OPEN);
  assert.equal(connections.length, 2);
  assert.equal(replacement.sent.some(message => message.id === 'old-session'), false);
});
