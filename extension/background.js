/* global JEV_BRIDGE_CONFIG */
importScripts("config.js");

let socket = null;
let reconnectTimer = null;

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

function pageAct(action, expectedPageKey, expectedGuard, expectedUrl, text) {
  const cache = window.__jevFast;
  if (!cache || location.href !== expectedUrl) return {stale: true};
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

    if (action.kind === "fill") {
      if (element.readOnly || element.getAttribute("aria-readonly") === "true") return {stale: true};
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
      if (element.tagName !== "SELECT" ||
          ![...element.options].some(option => option.value === action.value && !option.disabled)) {
        return {stale: true};
      }
      const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
      if (setter) setter.call(element, action.value); else element.value = action.value;
      element.dispatchEvent(new Event("input", {bubbles: true}));
      element.dispatchEvent(new Event("change", {bubbles: true}));
    } else if (action.kind === "click") {
      element.focus({preventScroll: true});
      element.click();
    }
  } else if (action.kind === "scroll") {
    window.scrollBy({top: action.delta, left: 0, behavior: "instant"});
  } else if (action.kind !== "wait") {
    return {stale: true};
  }
  return {executed: action.id};
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
    const results = await chrome.scripting.executeScript({
      target: {tabId},
      func: pageAct,
      args: [params.action, params.pageKey, params.guard ?? null, params.url, params.text ?? null],
      world: "ISOLATED"
    });
    const value = results?.[0]?.result;
    if (!value || value.stale) throw new Error("STALE_PAGE");
    await sleep(params.action?.kind === "fill" ? 200 : 70);
    return value;
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
  socket = new WebSocket(`ws://127.0.0.1:${config.port}/${config.token}`);
  socket.onopen = () => socket.send(JSON.stringify({
    type: "hello", extensionId: chrome.runtime.id, version: chrome.runtime.getManifest().version
  }));
  socket.onmessage = async event => {
    let command;
    try {
      command = JSON.parse(event.data);
      if (command.type !== "command" || !command.id) return;
      const result = await execute(command);
      socket.send(JSON.stringify({type: "response", id: command.id, result}));
    } catch (error) {
      if (socket?.readyState === WebSocket.OPEN && command?.id) {
        socket.send(JSON.stringify({type: "response", id: command.id, error: String(error?.message || error)}));
      }
    }
  };
  socket.onclose = scheduleReconnect;
  socket.onerror = () => socket?.close();
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
