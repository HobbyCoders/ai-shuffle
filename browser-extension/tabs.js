// The tabs an agent may drive: the tabs inside a chat's tab group (the ones
// this extension opened there, the tabs those opened, and any the person moved
// into the group before the chat's browser last started), attached with
// chrome.debugger while that chat's browser is running. Nothing outside the
// group is attached or readable, and a tab dragged out of the group is let go
// at once.

const GROUP_COLOR = "blue"; // matches the AI Shuffle icon

/**
 * chatGroupKey (worker id + chat id) -> { groupId, title, tabs: Set<tabId>, worker, active }
 * `active`: the chat's browser runtime is connected and may drive the tabs.
 * A group stays known after its runtime stops (the tabs and the Chrome group
 * stay open for the person), so the next runtime picks the same group up.
 */
const groups = new Map();
/** tabId -> chatGroupKey */
const tabOwner = new Map();
/**
 * Tabs the person pressed Cancel on Chrome's debugging bar for. They stay in
 * their group but are never attached again, across runtime and socket
 * restarts, until the person takes them out of the group (or closes them):
 * putting one back in is how they hand it to the agent again.
 */
const declined = new Set();
let eventSink = () => {};

export function onEvent(sink) {
  eventSink = sink;
}

/** Tabs an agent can drive right now (the toolbar badge). */
export function ownedTabCount() {
  let count = 0;
  for (const g of groups.values()) if (g.active) count += g.tabs.size;
  return count;
}

/** The chat group key that owns `tabId`, or undefined. */
export function groupOf(tabId) {
  return tabOwner.get(tabId);
}

export function groupSummaries() {
  return [...groups.values()].filter((g) => g.active).map((g) => ({ title: g.title, worker: g.worker, tabs: g.tabs.size }));
}

async function persist() {
  const saved = {};
  for (const [key, g] of groups) {
    saved[key] = { groupId: g.groupId, title: g.title, worker: g.worker, tabs: [...g.tabs], active: g.active };
  }
  await chrome.storage.session.set({ groups: saved, declined: [...declined] });
}

/** Re-learn groups after the service worker restarts (tabs survive it). */
export async function restore() {
  const { groups: saved = {}, declined: refused = [] } = await chrome.storage.session.get(["groups", "declined"]);
  const open = new Set((await chrome.tabs.query({})).map((t) => t.id));
  for (const id of refused) if (open.has(id)) declined.add(id);
  for (const [key, g] of Object.entries(saved)) {
    const tabs = new Set(g.tabs.filter((id) => open.has(id)));
    groups.set(key, { groupId: g.groupId, title: g.title, worker: g.worker, tabs, active: !!g.active });
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

/** The Chrome group id if that group still exists, else null. */
async function liveGroupId(g) {
  if (g.groupId == null) return null;
  try {
    await chrome.tabGroups.get(g.groupId);
    return g.groupId;
  } catch {
    return null;
  }
}

async function addToGroup(key, tabId) {
  const g = groups.get(key);
  let groupId = await liveGroupId(g);
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
    g = { groupId: null, title, worker, tabs: new Set(), active: false };
    groups.set(key, g);
  } else if (title && g.title !== title) {
    g.title = title;
    if (g.groupId != null) chrome.tabGroups.update(g.groupId, { title }).catch(() => {});
  }
  return g;
}

function forgetTab(key, tabId) {
  groups.get(key)?.tabs.delete(tabId);
  if (tabOwner.get(tabId) === key) tabOwner.delete(tabId);
}

/** The chat's tabs, attached again when its browser runtime (re)starts. */
export async function attachGroup(key, title, worker) {
  const g = ensureGroup(key, title, worker);
  g.active = true;
  // Pick up what is in the Chrome group now: the tabs left there when the
  // last runtime stopped, minus any the person took out, plus any they added.
  const groupId = await liveGroupId(g);
  g.groupId = groupId;
  const current = groupId == null ? [] : await chrome.tabs.query({ groupId });
  const inGroup = new Set(current.map((t) => t.id));
  for (const tabId of [...g.tabs]) if (!inGroup.has(tabId)) forgetTab(key, tabId);
  const tabs = [];
  for (const tabId of inGroup) {
    const other = tabOwner.get(tabId);
    if ((other && other !== key) || declined.has(tabId)) continue;
    try {
      tabs.push({ tabId, targetInfo: await attach(tabId) });
      g.tabs.add(tabId);
      tabOwner.set(tabId, key);
    } catch {
      forgetTab(key, tabId);
    }
  }
  await persist();
  return { tabs };
}

export async function createTab(key, title, worker, url) {
  const g = ensureGroup(key, title, worker);
  g.active = true;
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

/** The tab is this chat's, its runtime is running and the tab is still in the group. */
async function owned(key, tabId) {
  const g = groups.get(key);
  if (tabOwner.get(tabId) !== key || !g?.active) throw new Error("That tab is not in this chat's tab group");
  let tab = null;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch {
    /* closed */
  }
  if (!tab || g.groupId == null || tab.groupId !== g.groupId) {
    await letGo(key, tabId, "left_group");
    throw new Error("That tab is not in this chat's tab group");
  }
}

export async function closeTab(key, tabId) {
  await owned(key, tabId);
  await chrome.tabs.remove(tabId);
}

export async function activateTab(key, tabId) {
  await owned(key, tabId);
  // Select the tab in its window without raising the window over whatever
  // the person is doing.
  await chrome.tabs.update(tabId, { active: true });
}

export async function command(key, tabId, sessionId, method, params) {
  await owned(key, tabId);
  const target = sessionId ? { tabId, sessionId } : { tabId };
  return (await chrome.debugger.sendCommand(target, method, params || {})) || {};
}

/** Stop driving one tab (it left the group): detach and tell the worker. */
async function letGo(key, tabId, reason) {
  if (tabOwner.get(tabId) !== key) return;
  const wasActive = !!groups.get(key)?.active;
  forgetTab(key, tabId);
  await chrome.debugger.detach({ tabId }).catch(() => {});
  await persist();
  if (wasActive) eventSink(key, { event: "detached", tabId, reason });
}

/** The chat's runtime stopped: detach its tabs; the tabs and the group stay for the person. */
export async function releaseGroup(key) {
  const g = groups.get(key);
  if (!g) return;
  g.active = false;
  for (const tabId of g.tabs) await chrome.debugger.detach({ tabId }).catch(() => {});
  await persist();
}

/**
 * A worker's socket dropped: detach its chats' tabs but keep their groups, so
 * the runtimes that start after it reconnects pick the same groups up.
 */
export async function suspendWorker(worker) {
  for (const [key, g] of [...groups]) if (g.worker === worker) await releaseGroup(key);
}

/** Stop everything (the toolbar's Stop or sign-out): no chat keeps a group. */
export async function releaseWorker(worker) {
  for (const [key, g] of [...groups]) {
    if (worker && g.worker !== worker) continue;
    await releaseGroup(key);
    for (const tabId of g.tabs) if (tabOwner.get(tabId) === key) tabOwner.delete(tabId);
    groups.delete(key);
  }
  await persist();
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  const key = tabOwner.get(source.tabId);
  if (key && groups.get(key)?.active) {
    eventSink(key, { event: "cdp", tabId: source.tabId, sessionId: source.sessionId, method, params });
  }
});

chrome.debugger.onDetach.addListener((source, reason) => {
  const key = tabOwner.get(source.tabId);
  if (!key || !groups.get(key)?.active) return;
  // "canceled_by_user": the person pressed Cancel on Chrome's debugging bar.
  if (reason === "canceled_by_user") declined.add(source.tabId);
  forgetTab(key, source.tabId);
  persist();
  eventSink(key, { event: "detached", tabId: source.tabId, reason });
});

chrome.tabs.onRemoved.addListener((tabId) => {
  if (declined.delete(tabId)) persist();
  const key = tabOwner.get(tabId);
  if (!key) return;
  const wasActive = !!groups.get(key)?.active;
  forgetTab(key, tabId);
  persist();
  if (wasActive) eventSink(key, { event: "detached", tabId, reason: "tab_closed" });
});

// A tab the person drags out of a chat's group is let go at once.
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (!("groupId" in changeInfo)) return;
  // Taking a declined tab out of its group ends the refusal: moving it back
  // in later is the person handing it to the agent again.
  if (declined.has(tabId) && ![...groups.values()].some((g) => g.groupId === changeInfo.groupId)) {
    declined.delete(tabId);
    persist();
  }
  const key = tabOwner.get(tabId);
  const g = key && groups.get(key);
  if (g && changeInfo.groupId !== g.groupId) letGo(key, tabId, "left_group").catch(() => {});
});

// A popup or link opened from a driven tab joins that chat's group.
chrome.tabs.onCreated.addListener(async (tab) => {
  const key = tab.openerTabId != null ? tabOwner.get(tab.openerTabId) : null;
  if (!key || !groups.get(key)?.active) return;
  try {
    await addToGroup(key, tab.id);
    const info = await attach(tab.id);
    eventSink(key, { event: "tab_created", tabId: tab.id, targetInfo: info });
  } catch {
    /* the tab closed before it could be attached */
  }
});
