// Keeps one socket open to each of the account's workers so their chats can
// pick this browser, and runs what a chat's browser runtime asks for inside
// that chat's tab group (tabs.js). See app/core/browser_extension.py on the
// worker for the protocol.

import * as account from "./account.js";
import * as tabs from "./tabs.js";

const PING_MS = 20000;
const RETRY_MS = [2000, 5000, 15000, 30000, 60000];

/** worker id -> { socket, state, detail, attempts, timer, ping } */
const connections = new Map();
let paused = false;
let ready = null;

function groupKey(workerId, group) {
  return `${workerId}:${group}`;
}

function splitKey(key) {
  const at = key.indexOf(":");
  return [key.slice(0, at), key.slice(at + 1)];
}

tabs.onEvent((key, message) => {
  const [workerId, group] = splitKey(key);
  const conn = connections.get(workerId);
  updateBadge();
  if (conn?.state !== "connected") return;
  send(conn, message.event === "tab_created" ? { ...message, group } : message);
});

function send(conn, message) {
  try {
    conn.socket.send(JSON.stringify(message));
  } catch {
    /* closing; the close handler reconnects */
  }
}

/** Toolbar badge: how many tabs agents are driving, "off" when paused. */
function updateBadge() {
  const live = [...connections.values()].some((c) => c.state === "connected");
  const driving = tabs.ownedTabCount();
  chrome.action.setBadgeBackgroundColor({ color: driving ? "#0289F9" : "#6b7280" });
  chrome.action.setBadgeText({ text: paused ? "off" : driving ? String(driving) : live || !connections.size ? "" : "!" });
}

/** Connection or sign-in state changed: badge, and the side panel re-renders. */
function broadcastStatus() {
  updateBadge();
  chrome.runtime.sendMessage({ type: "status-changed" }).catch(() => {});
}

async function handle(workerId, request) {
  const p = request.params || {};
  const key = groupKey(workerId, String(p.group || ""));
  switch (request.method) {
    case "group.attach":
      return tabs.attachGroup(key, String(p.title || "AI Shuffle"), workerId);
    case "tab.create":
      return tabs.createTab(key, String(p.title || "AI Shuffle"), workerId, p.url);
    case "group.release":
      return tabs.releaseGroup(key);
    case "tab.close":
    case "tab.activate":
    case "cdp": {
      const owner = tabs.groupOf(p.tabId);
      if (!owner || splitKey(owner)[0] !== workerId) throw new Error("That tab is not in this chat's tab group");
      if (request.method === "tab.close") return tabs.closeTab(owner, p.tabId);
      if (request.method === "tab.activate") return tabs.activateTab(owner, p.tabId);
      return tabs.command(owner, p.tabId, p.sessionId, p.method, p.params);
    }
    default:
      throw new Error(`Unknown request ${request.method}`);
  }
}

function connect(worker) {
  const existing = connections.get(worker.id);
  if (existing?.socket && existing.socket.readyState <= WebSocket.OPEN) return;
  const conn = existing || { attempts: 0 };
  connections.set(worker.id, conn);
  clearTimeout(conn.timer);
  conn.name = worker.name;
  conn.state = "connecting";
  conn.detail = "";
  const url = new URL("/api/v1/browser-extension/connect", worker.origin);
  url.protocol = url.protocol === "http:" ? "ws:" : "wss:";
  const socket = new WebSocket(url);
  conn.socket = socket;
  socket.onopen = async () => {
    const info = account.browserInfo();
    const { device_id } = await account.identity();
    socket.send(JSON.stringify({ event: "hello", key: worker.key, browser_id: device_id, ...info }));
  };
  socket.onmessage = async (frame) => {
    let message;
    try {
      message = JSON.parse(frame.data);
    } catch {
      return;
    }
    if (message.event === "welcome") {
      conn.state = "connected";
      conn.browserId = typeof message.browser_id === "string" ? message.browser_id : null;
      conn.attempts = 0;
      conn.ping = setInterval(() => send(conn, { event: "ping" }), PING_MS);
      broadcastStatus();
      return;
    }
    if (message.event === "refused") {
      conn.refused = message;
      conn.detail = message.detail || "Refused";
      return;
    }
    if (message.event === "pong") return;
    if (message.id != null && message.method) {
      try {
        const result = await handle(worker.id, message);
        send(conn, { id: message.id, result: result ?? {} });
      } catch (error) {
        send(conn, { id: message.id, error: String(error?.message || error) });
      }
      updateBadge();
    }
  };
  socket.onclose = async (event) => {
    clearInterval(conn.ping);
    if (conn.socket !== socket) return;
    conn.socket = null;
    await tabs.releaseWorker(worker.id);
    const refused = conn.refused;
    conn.refused = null;
    if (paused) {
      conn.state = "off";
      broadcastStatus();
      return;
    }
    conn.state = refused ? "refused" : "offline";
    if (!refused) conn.detail = event.code === 1006 ? "Worker unreachable" : conn.detail || "Disconnected";
    broadcastStatus();
    if (refused?.status === 401) {
      // The key expired or was revoked: enroll again through the account.
      try {
        const fresh = await account.enroll({ id: worker.id, name: worker.name, url: worker.url || worker.origin });
        await saveWorker({ ...fresh, url: worker.url });
        connect(fresh);
        return;
      } catch (error) {
        conn.detail = error.message;
        broadcastStatus();
      }
    }
    if (refused?.status === 403) return; // waits for the owner to confirm this browser
    const delay = RETRY_MS[Math.min(conn.attempts++, RETRY_MS.length - 1)];
    conn.timer = setTimeout(() => connect(worker), delay);
  };
}

async function savedWorker(workerId) {
  const { workers = {} } = await chrome.storage.local.get("workers");
  const worker = workers[workerId];
  if (!worker?.key) throw new Error(worker?.error || "This browser is not connected to that worker");
  return worker;
}

/**
 * A web session for the side panel's copy of the worker's phone UI. Kept for
 * the browser session and shared by every window's panel: minting another
 * replaces the previous one on the worker.
 */
async function webSession(workerId, { fresh = false } = {}) {
  const { webSessions = {} } = await chrome.storage.session.get("webSessions");
  const cached = webSessions[workerId];
  if (!fresh && cached && Date.parse(cached.expires_at) > Date.now() + 120000) return cached;
  let worker = await savedWorker(workerId);
  let minted;
  try {
    minted = await account.call(worker.origin, "/api/v1/mobile/web-session", {
      method: "POST",
      token: worker.key,
      body: { purpose: "web" },
    });
  } catch (error) {
    if (error.status !== 401) throw error;
    worker = { ...(await account.enroll({ id: worker.id, name: worker.name, url: worker.url || worker.origin })), url: worker.url };
    await saveWorker(worker);
    connections.get(worker.id)?.socket?.close(1000, "New key");
    minted = await account.call(worker.origin, "/api/v1/mobile/web-session", {
      method: "POST",
      token: worker.key,
      body: { purpose: "web" },
    });
  }
  if (typeof minted.session_token !== "string" || !minted.session_token) {
    throw new Error(`${worker.name} returned an unsupported answer. Update that worker.`);
  }
  const session = { token: minted.session_token, expires_at: minted.expires_at, origin: worker.origin };
  webSessions[workerId] = session;
  await chrome.storage.session.set({ webSessions });
  return session;
}

/** Let a chat the side panel shows use this browser, unless it already picked one. */
async function claimSession(workerId, sessionId) {
  const conn = connections.get(workerId);
  if (paused || conn?.state !== "connected" || !conn.browserId) return null;
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(sessionId)) throw new Error("Invalid chat id");
  const { token, origin } = await webSession(workerId);
  return account.call(origin, `/api/v1/sessions/${encodeURIComponent(sessionId)}/browser/target`, {
    method: "PUT",
    token,
    body: { browser_id: conn.browserId, only_if_unassigned: true },
  });
}

async function saveWorker(worker) {
  const { workers = {} } = await chrome.storage.local.get("workers");
  workers[worker.id] = worker;
  await chrome.storage.local.set({ workers });
}

function disconnectAll() {
  for (const conn of connections.values()) {
    clearTimeout(conn.timer);
    conn.socket?.close(1000, "Paused");
  }
}

async function start() {
  ({ paused = false } = await chrome.storage.local.get("paused"));
  await tabs.restore();
  if (paused || !(await account.session())) {
    broadcastStatus();
    return;
  }
  let workers;
  try {
    workers = await account.syncWorkers();
  } catch {
    ({ workers = {} } = await chrome.storage.local.get("workers"));
  }
  for (const worker of Object.values(workers)) if (worker.key) connect(worker);
  broadcastStatus();
}

function boot() {
  ready = start().catch(() => {});
  return ready;
}

async function status() {
  await ready;
  const current = await account.session();
  const { workers = {} } = await chrome.storage.local.get("workers");
  return {
    signedIn: Boolean(current),
    username: current?.username || "",
    paused,
    browser: account.browserInfo(),
    groups: tabs.groupSummaries(),
    workers: Object.values(workers).map((w) => {
      const c = connections.get(w.id);
      return { id: w.id, name: w.name, state: w.key ? c?.state || "offline" : "error", detail: c?.detail || w.error || "" };
    }),
  };
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id) return false;
  (async () => {
    switch (message?.type) {
      case "status":
        return status();
      case "sign-in":
        await account.signIn({ createAccount: Boolean(message.createAccount) });
        paused = false;
        await chrome.storage.local.set({ paused });
        disconnectAll();
        connections.clear();
        await boot();
        return status();
      case "sign-out":
        paused = true;
        disconnectAll();
        await tabs.releaseWorker();
        await account.signOut();
        connections.clear();
        paused = false;
        return status();
      case "pause":
        paused = Boolean(message.paused);
        await chrome.storage.local.set({ paused });
        if (paused) {
          disconnectAll();
          await tabs.releaseWorker();
        } else {
          await boot();
        }
        return status();
      case "refresh":
        disconnectAll();
        connections.clear();
        await boot();
        return status();
      case "web-session":
        return webSession(String(message.workerId || ""));
      case "claim-session":
        return claimSession(String(message.workerId || ""), String(message.sessionId || ""));
      default:
        throw new Error("Unknown message");
    }
  })().then(
    (result) => reply({ ok: true, result }),
    (error) => reply({ ok: false, error: String(error?.message || error) }),
  );
  return true;
});

// MV3 may stop the service worker; an alarm brings it back to reconnect.
chrome.alarms.create("keepalive", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(() => {
  if (!paused && ![...connections.values()].some((c) => c.socket)) boot();
});
chrome.runtime.onStartup.addListener(() => {});
// The toolbar button opens the side panel (sign-in, workers, chats).
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
boot();
