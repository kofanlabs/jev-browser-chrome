const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

// Exercise the shipped service worker with Chrome APIs stubbed, without a browser.
function bridge(create) {
  const listener = {addListener() {}};
  const context = vm.createContext({
    URL, importScripts() {}, self: {}, setInterval() {},
    chrome: {
      tabs: {create},
      alarms: {create() {}, onAlarm: listener},
      runtime: {onInstalled: listener, onStartup: listener},
    },
  });
  vm.runInContext(readFileSync(join(__dirname, '../extension/background.js'), 'utf8'), context);
  return params => context.execute({method: 'create_tab', params});
}

for (const active of [true, false]) {
  test(`creates one tab, preserving active=${active} and pending navigation`, async () => {
    const calls = [];
    const execute = bridge(async options => {
      calls.push({...options});
      return {id: 123, url: '', pendingUrl: options.url, active: options.active, windowId: 9};
    });
    const result = await execute({url: 'https://example.com', active});
    assert.deepEqual(calls, [{url: 'https://example.com/', active}]);
    assert.deepEqual({...result}, {tabId: '123', url: 'https://example.com/', title: '', active, windowId: 9});
  });
}

for (const url of ['file:///secret', 'javascript:alert(1)', 'https://user:pass@example.com', 'invalid']) {
  test(`rejects ${url} before calling Chrome`, async () => {
    let calls = 0;
    const execute = bridge(async () => { calls++; });
    await assert.rejects(execute({url}));
    assert.equal(calls, 0);
  });
}

test('does not retry an uncertain create failure', async () => {
  let calls = 0;
  const execute = bridge(async () => { calls++; throw new Error('connection lost'); });
  await assert.rejects(execute({url: 'https://example.com'}), /connection lost/);
  assert.equal(calls, 1);
});
