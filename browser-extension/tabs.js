// The tabs an agent may drive: only tabs this extension opened (or that those
// tabs opened) inside a chat's tab group, attached with chrome.debugger.
// Nothing else in the browser is attached or readable.

const GROUP_COLOR = "blue"; // matches the AI Shuffle icon

/** chatGroupKey (worker id + chat id) -> { groupId, title, tabs: Set<tabId>, worker } */
const groups = new Map();
/** tabId -> chatGroupKey */
const tabOwner = new Map();
let eventSink = () => {};

export function onEvent(sink) {
  eventSink = sink;
}

export function ownedTabCount() {
  return tabOwner.size;
}

/** The chat group key that owns `tabId`, or undefined. */
export function groupOf(tabId) {
  return tabOwner.get(tabId);
}

export function groupSummaries() {
  return [...groups.values()].map((g) => ({ title: g.title, worker: g.worker, tabs: g.tabs.size }));
}

async function persist() {
  const saved = {};
  for (const [key, g] of groups) saved[key] = { groupId: g.groupId, title: g.title, worker: g.worker, tabs: [...g.tabs] };
  await chrome.storage.session.set({ groups: saved });
}

/** Re-learn groups after the service worker restarts (tabs survive it). */
export async function restore() {
  const { groups: saved = {} } = await chrome.storage.session.get("groups");
  const open = new Set((await chrome.tabs.query({})).map((t) => t.id));
  for (const [key, g] of Object.entries(saved)) {
    const tabs = new Set(g.tabs.filter((id) => open.has(id)));
    groups.set(key, { groupId: g.groupId, title: g.title, worker: g.worker, tabs });
    for (const id of tabs) tabOwner.set(id, key);
  }
}

async function targetInfo(tabId) {
  const { targetInfo } = await chrome.debugger.sendCommand({ tabId }, "Target.getTargetInfo");
  return targetInfo;
}

async function attach(tabId) {
  try {
    await chrome.debugger.attach({ tabId }, "1.3");
  } catch (error) {
    if (!/already attached/i.test(String(error?.message))) throw error;
  }
  return targetInfo(tabId);
}

async function addToGroup(key, tabId) {
  const g = groups.get(key);
  let groupId = g.groupId;
  try {
    if (groupId != null) await chrome.tabGroups.get(groupId);
  } catch {
    groupId = null;
  }
  if (groupId == null) {
    groupId = await chrome.tabs.group({ tabIds: [tabId] });
    await chrome.tabGroups.update(groupId, { title: g.title, color: GROUP_COLOR, collapsed: false });
  } else {
    await chrome.tabs.group({ groupId, tabIds: [tabId] });
  }
  g.groupId = groupId;
  g.tabs.add(tabId);
  tabOwner.set(tabId, key);
  await persist();
}

function ensureGroup(key, title, worker) {
  let g = groups.get(key);
  if (!g) {
    g = { groupId: null, title, worker, tabs: new Set() };
    groups.set(key, g);
  } else if (title && g.title !== title) {
    g.title = title;
    if (g.groupId != null) chrome.tabGroups.update(g.groupId, { title }).catch(() => {});
  }
  return g;
}

/** The chat's existing tabs, attached again (after a runtime restart). */
export async function attachGroup(key, title, worker) {
  const g = ensureGroup(key, title, worker);
  const tabs = [];
  for (const tabId of [...g.tabs]) {
    try {
      tabs.push({ tabId, targetInfo: await attach(tabId) });
    } catch {
      g.tabs.delete(tabId);
      tabOwner.delete(tabId);
    }
  }
  await persist();
  return { tabs };
}

export async function createTab(key, title, worker, url) {
  ensureGroup(key, title, worker);
  const safe = /^(https?:|about:blank$)/i.test(url || "") ? url : "about:blank";
  // Shown, not in the background: a hidden tab is throttled and cannot be
  // screenshotted reliably, and the person should see what the agent opened.
  const tab = await chrome.tabs.create({ url: "about:blank", active: true });
  await addToGroup(key, tab.id);
  const info = await attach(tab.id);
  if (safe !== "about:blank") {
    chrome.debugger.sendCommand({ tabId: tab.id }, "Page.navigate", { url: safe }).catch(() => {});
  }
  return { tabId: tab.id, targetInfo: info };
}

function owned(key, tabId) {
  if (tabOwner.get(tabId) !== key) throw new Error("That tab is not in this chat's tab group");
}

export async function closeTab(key, tabId) {
  owned(key, tabId);
  await chrome.tabs.remove(tabId);
}

export async function activateTab(key, tabId) {
  owned(key, tabId);
  // Select the tab in its window without raising the window over whatever
  // the person is doing.
  await chrome.tabs.update(tabId, { active: true });
}

export async function command(key, tabId, sessionId, method, params) {
  owned(key, tabId);
  const target = sessionId ? { tabId, sessionId } : { tabId };
  return (await chrome.debugger.sendCommand(target, method, params || {})) || {};
}

/** Stop driving a chat's tabs; the tabs and the group stay for the person. */
export async function releaseGroup(key) {
  const g = groups.get(key);
  if (!g) return;
  for (const tabId of g.tabs) {
    tabOwner.delete(tabId);
    await chrome.debugger.detach({ tabId }).catch(() => {});
  }
  groups.delete(key);
  await persist();
}

/** Stop everything (the toolbar's Stop, sign-out or a worker disconnect). */
export async function releaseWorker(worker) {
  for (const [key, g] of [...groups]) if (!worker || g.worker === worker) await releaseGroup(key);
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  const key = tabOwner.get(source.tabId);
  if (key) eventSink(key, { event: "cdp", tabId: source.tabId, sessionId: source.sessionId, method, params });
});

chrome.debugger.onDetach.addListener((source, reason) => {
  const key = tabOwner.get(source.tabId);
  if (!key) return;
  // "canceled_by_user": the person pressed Cancel on Chrome's debugging bar.
  const g = groups.get(key);
  g?.tabs.delete(source.tabId);
  tabOwner.delete(source.tabId);
  persist();
  eventSink(key, { event: "detached", tabId: source.tabId, reason });
});

chrome.tabs.onRemoved.addListener((tabId) => {
  const key = tabOwner.get(tabId);
  if (!key) return;
  groups.get(key)?.tabs.delete(tabId);
  tabOwner.delete(tabId);
  persist();
  eventSink(key, { event: "detached", tabId, reason: "tab_closed" });
});

// A popup or link opened from a chat's tab joins that chat's group.
chrome.tabs.onCreated.addListener(async (tab) => {
  const key = tab.openerTabId != null ? tabOwner.get(tab.openerTabId) : null;
  if (!key) return;
  try {
    await addToGroup(key, tab.id);
    const info = await attach(tab.id);
    eventSink(key, { event: "tab_created", tabId: tab.id, targetInfo: info });
  } catch {
    /* the tab closed before it could be attached */
  }
});
