/* global JEV_BRIDGE_CONFIG */
importScripts("config.js");

let socket = null;
let reconnectTimer = null;
let actionWatchSequence = 0;

const ACTION_SETTLE_MS = 3500;
const DOM_SETTLE_MS = 700;
const DOM_QUIET_MS = 60;

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function normalTab(tab) {
  return tab && Number.isInteger(tab.id) && /^https?:\/\//.test(tab.url || "");
}

async function runSnapshot(tabId) {
  const results = await chrome.scripting.executeScript({
    target: {tabId},
    files: ["snapshot.js"],
    world: "ISOLATED"
  });
  const value = results?.[0]?.result;
  if (!value) throw new Error("The page is still loading or cannot be read by the extension.");
  return value;
}

function pageAct(action, expectedPageKey, expectedGuard, expectedUrl, text, watchId) {
  const cache = window.__jevFast;
  if (!cache || location.href !== expectedUrl) return {stale: true};
  let baseline = null;
  const visibleFingerprint = () => {
    const selector = 'a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
      '[role="button"],[role="link"],[role="checkbox"],[role="radio"],[role="switch"],'+
      '[role="tab"],[role="menuitem"],[role="option"],[role="combobox"],[role="textbox"],'+
      '[role="searchbox"],[role="spinbutton"],[role="gridcell"]';
    const controls = [...document.querySelectorAll(selector)].slice(0, 500).map(e => {
      if (['password', 'file', 'hidden'].includes(e.type) ||
          e.closest('[aria-hidden="true"],[inert]') ||
          !e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return null;
      const bounds = e.getBoundingClientRect();
      if (!bounds.width || !bounds.height || bounds.bottom <= 0 || bounds.top >= innerHeight ||
          bounds.right <= 0 || bounds.left >= innerWidth) return null;
      return [e.tagName, e.getAttribute('role'), e.getAttribute('aria-label'),
        e.innerText?.slice(0, 240) || '', e.value ?? null, e.checked ?? null,
        e.selectedIndex ?? null, e.matches(':disabled'), e.getAttribute('aria-disabled'),
        e.getAttribute('aria-expanded'), e.getAttribute('aria-checked'),
        e.getAttribute('aria-selected'), e.getAttribute('href'), e.getAttribute('title'),
        e.getAttribute('alt')];
    }).filter(Boolean).slice(0, 250);
    const state = JSON.stringify([location.href, document.title, scrollX, scrollY,
      innerWidth, innerHeight, document.body?.innerText?.slice(0, 8000) || '', controls]);
    let first = 0x811c9dc5;
    let second = 0x9e3779b9;
    for (let index = 0; index < state.length; index += 1) {
      const code = state.charCodeAt(index);
      first = Math.imul(first ^ code, 0x01000193);
      second = Math.imul(second ^ (code + first), 0x85ebca6b);
    }
    return `${first >>> 0}:${second >>> 0}:${state.length}`;
  };
  const pageBusy = () => {
    const busyNode = [...document.querySelectorAll('[aria-busy="true"],[role="progressbar"]')]
      .some(e => {
        if (!e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return false;
        const bounds = e.getBoundingClientRect();
        return bounds.width > 0 && bounds.height > 0 && bounds.bottom > 0 && bounds.top < innerHeight &&
          bounds.right > 0 && bounds.left < innerWidth;
      });
    return busyNode;
  };
  if (Number.isInteger(action.node)) {
    const element = cache.nodes.get(action.node);
    const samePage = JSON.stringify(cache.pageKey()) === JSON.stringify(expectedPageKey);
    const sameGuard = JSON.stringify(cache.guard(element)) === JSON.stringify(expectedGuard);
    if (!samePage || !sameGuard) return {stale: true};
    if (!element?.isConnected || element.matches(":disabled") ||
        element.closest('[aria-disabled="true"],[inert]') ||
        !element.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) {
      return {stale: true};
    }
    const rect = element.getBoundingClientRect();
    const x = rect.x + rect.width / 2;
    const y = rect.y + rect.height / 2;
    if (!rect.width || !rect.height || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight ||
        !element.contains(document.elementFromPoint(x, y))) return {stale: true};
    if (action.kind === "fill" &&
        (element.readOnly || element.getAttribute("aria-readonly") === "true")) return {stale: true};
    if (action.kind === "select" && (element.tagName !== "SELECT" ||
        ![...element.options].some(option => option.value === action.value && !option.disabled))) {
      return {stale: true};
    }
    if (!["fill", "select", "click"].includes(action.kind)) return {stale: true};
    const previousWatch = window.__jevActionWatch;
    previousWatch?.observer?.disconnect();
    baseline = visibleFingerprint();
    window.__jevActionWatch = {id: watchId, signature: visibleFingerprint, busy: pageBusy};

    if (action.kind === "fill") {
      element.focus();
      if (element.isContentEditable) {
        element.textContent = text;
      } else {
        const proto = element.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
        if (setter) setter.call(element, text); else element.value = text;
      }
      element.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: text}));
      element.dispatchEvent(new Event("change", {bubbles: true}));
    } else if (action.kind === "select") {
      const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
      if (setter) setter.call(element, action.value); else element.value = action.value;
      element.dispatchEvent(new Event("input", {bubbles: true}));
      element.dispatchEvent(new Event("change", {bubbles: true}));
    } else if (action.kind === "click") {
      element.focus({preventScroll: true});
      element.click();
    }
  } else if (action.kind === "scroll") {
    const previousWatch = window.__jevActionWatch;
    previousWatch?.observer?.disconnect();
    baseline = visibleFingerprint();
    window.__jevActionWatch = {id: watchId, signature: visibleFingerprint, busy: pageBusy};
    window.scrollBy({top: action.delta, left: 0, behavior: "instant"});
  } else if (action.kind !== "wait") {
    return {stale: true};
  }
  const watch = window.__jevActionWatch;
  return {
    executed: action.id,
    watchId,
    baseline: watch?.id === watchId ? baseline : null,
    immediate: watch?.id === watchId ? watch.signature() : null,
    immediateBusy: watch?.id === watchId ? watch.busy() : false
  };
}

function readActionWatch(watchId) {
  const watch = window.__jevActionWatch;
  if (!watch || watch.id !== watchId || typeof watch.signature !== "function") return null;
  return {signature: watch.signature(), busy: Boolean(watch.busy?.())};
}

function stopActionWatch(watchId) {
  const watch = window.__jevActionWatch;
  if (watch?.id !== watchId) return false;
  watch.observer?.disconnect();
  delete window.__jevActionWatch;
  return true;
}

function watchNavigation(tabId, expectedUrl) {
  const state = {started: false};
  const listener = (changedTabId, changeInfo, tab) => {
    if (changedTabId !== tabId) return;
    if (changeInfo.status === "loading" || tab?.pendingUrl ||
        (changeInfo.url && changeInfo.url !== expectedUrl) || (tab?.url && tab.url !== expectedUrl)) {
      state.started = true;
    }
  };
  const updates = chrome.tabs.onUpdated;
  updates?.addListener?.(listener);
  return {
    state,
    inspect(tab) {
      if (tab?.status === "loading" || tab?.pendingUrl || (tab?.url && tab.url !== expectedUrl)) {
        state.started = true;
      }
    },
    close() { updates?.removeListener?.(listener); }
  };
}

async function settleAction(tabId, action, expectedNavigation, result, navigation) {
  const startedAt = Date.now();
  const deadline = startedAt + (action.kind === "click" ? ACTION_SETTLE_MS : DOM_SETTLE_MS);
  let lastSignature = result.immediate || result.baseline;
  let lastSignatureChange = result.baseline !== lastSignature ? Date.now() : null;
  let visibleChanged = lastSignatureChange !== null;
  let busy = Boolean(result.immediateBusy);
  let delay = 20;

  while (Date.now() < deadline) {
    let tab;
    try {
      tab = await chrome.tabs.get(tabId);
    } catch {
      return {settled: false, settlement: "tab-unavailable"};
    }
    if (tab.frozen) return {settled: false, settlement: "frozen"};
    navigation.inspect(tab);
    if (navigation.state.started && tab.status === "complete" && !tab.pendingUrl) {
      return {settled: true, settlement: "navigation-complete", url: tab.url};
    }

    if (!navigation.state.started) {
      let observed;
      try {
        const values = await chrome.scripting.executeScript({
          target: {tabId}, func: readActionWatch, args: [result.watchId], world: "ISOLATED"
        });
        observed = values?.[0]?.result;
      } catch {
        return {settled: false, settlement: "observation-unavailable"};
      }
      if (observed?.signature && observed.signature !== lastSignature) {
        lastSignature = observed.signature;
        lastSignatureChange = Date.now();
        visibleChanged = true;
      }
      busy = Boolean(observed?.busy);
      if (visibleChanged && !busy && !expectedNavigation &&
          Date.now() - lastSignatureChange >= DOM_QUIET_MS) {
        return {settled: true, settlement: "visible-change"};
      }
    }
    await sleep(delay);
    delay = Math.min(Math.round(delay * 1.6), 180);
  }
  if (navigation.state.started) return {settled: false, settlement: "navigation-timeout"};
  if (busy) return {settled: false, settlement: "busy-timeout"};
  if (visibleChanged) return {settled: false, settlement: "dom-change-inconclusive"};
  return {settled: false, settlement: "no-visible-change"};
}

async function execute(command) {
  const params = command.params || {};
  if (command.method === "status") {
    return {connected: true, extensionVersion: chrome.runtime.getManifest().version};
  }
  if (command.method === "list_tabs") {
    const tabs = (await chrome.tabs.query({})).filter(normalTab);
    return {tabs: tabs.map(tab => ({
      tabId: String(tab.id), url: tab.url, title: tab.title || "",
      active: Boolean(tab.active), windowId: tab.windowId
    }))};
  }
  if (command.method === "create_tab") {
    const url = new URL(params.url);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
      throw new Error("Only HTTP(S) URLs without embedded credentials are supported.");
    }
    const tab = await chrome.tabs.create({url: url.href, active: params.active !== false});
    return {
      tabId: String(tab.id), url: tab.pendingUrl || tab.url || url.href,
      title: tab.title || "", active: Boolean(tab.active), windowId: tab.windowId
    };
  }
  const tabId = Number(params.tabId);
  const tab = await chrome.tabs.get(tabId);
  if (!normalTab(tab)) throw new Error("The selected tab is unavailable or is not an HTTP(S) page.");
  if (command.method === "get_tab") {
    return {tabId: String(tab.id), url: tab.url, title: tab.title || "", active: Boolean(tab.active)};
  }
  if (command.method === "observe") {
    let lastError = null;
    for (let attempt = 0; attempt < 10; attempt += 1) {
      try {
        return await runSnapshot(tabId);
      } catch (error) {
        lastError = error;
        await sleep(30);
      }
    }
    throw lastError || new Error("The page could not be observed.");
  }
  if (command.method === "act") {
    if (params.action?.kind === "wait") {
      await sleep(100);
      return {executed: params.action.id};
    }
    if (tab.frozen) throw new Error("The selected tab is frozen; activate it before interacting.");
    const action = params.action;
    const watchId = `${tabId}:${Date.now()}:${++actionWatchSequence}`;
    const navigation = watchNavigation(tabId, params.url);
    const expectedNavigation = typeof params.guard?.[12] === "string" && params.guard[12].length > 0;
    try {
      const results = await chrome.scripting.executeScript({
        target: {tabId},
        func: pageAct,
        args: [action, params.pageKey, params.guard ?? null, params.url, params.text ?? null, watchId],
        world: "ISOLATED"
      });
      const value = results?.[0]?.result;
      if (value?.stale) throw new Error("STALE_PAGE");
      if (!value) throw new Error("ACTION_OUTCOME_UNKNOWN");
      if (value.executed !== action.id) throw new Error("ACTION_OUTCOME_UNKNOWN");

      let settlement;
      try {
        settlement = await settleAction(tabId, action, expectedNavigation, value, navigation);
      } catch {
        settlement = {settled: false, settlement: "observation-unavailable"};
      }
      try {
        await chrome.scripting.executeScript({
          target: {tabId}, func: stopActionWatch, args: [watchId], world: "ISOLATED"
        });
      } catch {
        // The action may have navigated, or the tab may have frozen. Its result is already confirmed.
      }
      return {executed: value.executed, ...settlement};
    } finally {
      navigation.close();
    }
  }
  if (command.method === "capture") {
    await chrome.tabs.update(tabId, {active: true});
    const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {format: "png"});
    return {data: dataUrl.split(",", 2)[1]};
  }
  throw new Error(`Unknown bridge method: ${command.method}`);
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, 1500);
}

function connect() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  const config = self.JEV_BRIDGE_CONFIG;
  if (!config?.token || !Number.isInteger(config.port)) return;
  const connection = new WebSocket(`ws://127.0.0.1:${config.port}/${config.token}`);
  socket = connection;
  connection.onopen = () => {
    if (socket !== connection || connection.readyState !== WebSocket.OPEN) return;
    connection.send(JSON.stringify({
      type: "hello", extensionId: chrome.runtime.id, version: chrome.runtime.getManifest().version
    }));
  };
  connection.onmessage = async event => {
    let command;
    try {
      command = JSON.parse(event.data);
      if (command.type !== "command" || !command.id) return;
      const result = await execute(command);
      if (socket === connection && connection.readyState === WebSocket.OPEN) {
        connection.send(JSON.stringify({type: "response", id: command.id, result}));
      }
    } catch (error) {
      if (socket === connection && connection.readyState === WebSocket.OPEN && command?.id) {
        connection.send(JSON.stringify({type: "response", id: command.id, error: String(error?.message || error)}));
      }
    }
  };
  connection.onclose = () => {
    if (socket !== connection) return;
    socket = null;
    scheduleReconnect();
  };
  connection.onerror = () => {
    if (socket === connection) connection.close();
  };
}

setInterval(() => {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({type: "heartbeat"}));
  } else {
    connect();
  }
}, 20000);

chrome.alarms.create("jev-reconnect", {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
connect();
