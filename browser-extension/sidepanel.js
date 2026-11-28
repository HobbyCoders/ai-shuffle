// The side panel: sign in, pick one of the account's workers, and use that
// worker's phone UI (its /mobile/ page) in a frame. The page gets a web
// session the extension minted with its device key. When the page shows a
// chat, the panel lets that chat use this browser unless a browser was
// already picked for it.

const $ = (id) => document.getElementById(id);
const EXTENSION_SESSION_MESSAGE = "shuffle-extension-session";

let state = null;
let shown = { worker: null, token: null };
let refreshTimer = null;

async function ask(type, extra = {}) {
  const reply = await chrome.runtime.sendMessage({ type, ...extra });
  if (!reply?.ok) throw new Error(reply?.error || "The extension did not answer");
  return reply.result;
}

function show(view) {
  for (const id of ["signed-out", "message", "app"]) $(id).hidden = id !== view;
  $("bar").hidden = view === "signed-out";
}

function message(title, text, action) {
  $("message-title").textContent = title;
  $("message-text").textContent = text;
  const button = $("message-action");
  button.hidden = !action;
  if (action) {
    button.textContent = action.label;
    button.onclick = action.run;
  }
  show("message");
  unloadFrame();
}

function unloadFrame() {
  if (shown.worker) $("app").src = "about:blank";
  shown = { worker: null, token: null };
}

async function selectedWorker() {
  const { panelWorker } = await chrome.storage.local.get("panelWorker");
  const workers = state.workers;
  return workers.find((w) => w.id === panelWorker) || workers.find((w) => w.state === "connected") || workers[0];
}

function renderBar(current) {
  const select = $("worker");
  select.replaceChildren(
    ...state.workers.map((w) => {
      const option = document.createElement("option");
      option.value = w.id;
      option.textContent = w.name;
      return option;
    }),
  );
  if (current) select.value = current.id;
  const dot = $("dot");
  dot.className = `dot ${current?.state || ""}`;
  dot.title = current ? describe(current) : "";
  $("pause").textContent = state.paused ? "Let agents use this browser" : "Stop agents using this browser";
  $("share-note").textContent = state.paused
    ? "Agents cannot use this browser right now."
    : state.groups.length
      ? `Agents are working in ${state.groups.length} tab group${state.groups.length === 1 ? "" : "s"}.`
      : `${state.browser.name} is available to your agents.`;
}

function describe(worker) {
  switch (worker.state) {
    case "connected":
      return "Connected";
    case "connecting":
      return "Connecting…";
    case "off":
      return "Browser sharing is off";
    default:
      return worker.detail || "Not connected";
  }
}

async function render() {
  try {
    state = await ask("status");
  } catch (error) {
    message("AI Shuffle could not start", error.message, { label: "Try again", run: render });
    return;
  }
  if (!state.signedIn) {
    unloadFrame();
    show("signed-out");
    return;
  }
  const current = await selectedWorker();
  renderBar(current);
  if (!current) {
    message(
      "No workers on this account",
      "Link a worker to your account in AI Shuffle (Settings → Account), then reload.",
      { label: "Reload workers", run: () => run("refresh") },
    );
    return;
  }
  if (current.state === "error") {
    message(`${current.name} is not available`, current.detail || "This browser could not connect to it.", {
      label: "Try again",
      run: () => run("refresh"),
    });
    return;
  }
  if (current.state === "refused" && /confirm/i.test(current.detail)) {
    message(
      `Confirm this browser on ${current.name}`,
      `Open AI Shuffle on ${current.name} and approve "${state.browser.name}" in Settings → Devices.`,
      { label: "Check again", run: () => run("refresh") },
    );
    return;
  }
  await openWorker(current);
}

async function openWorker(worker) {
  let session;
  try {
    session = await ask("web-session", { workerId: worker.id });
  } catch (error) {
    message(`Could not open ${worker.name}`, error.message, { label: "Try again", run: render });
    return;
  }
  show("app");
  if (shown.worker === worker.id && shown.token === session.token) return;
  shown = { worker: worker.id, token: session.token, origin: session.origin };
  const boot = { token: session.token, deviceName: state.browser.name, version: chrome.runtime.getManifest().version };
  $("app").src = `${session.origin}/mobile/#shuffle-extension=${encodeURIComponent(JSON.stringify(boot))}`;
  clearTimeout(refreshTimer);
  const renewIn = Date.parse(session.expires_at) - Date.now() - 60000;
  if (renewIn > 0 && renewIn < 2 ** 31) refreshTimer = setTimeout(render, renewIn);
}

async function run(type, extra) {
  $("menu").hidden = true;
  try {
    await ask(type, extra);
  } catch (error) {
    $("sign-in-error").textContent = error.message;
  }
  await render();
}

async function signIn(createAccount) {
  const buttons = [$("sign-in"), $("sign-up")];
  buttons.forEach((b) => (b.disabled = true));
  $("sign-in-error").textContent = "";
  try {
    await ask("sign-in", { createAccount });
  } catch (error) {
    $("sign-in-error").textContent = error.message;
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
  await render();
}

$("sign-in").onclick = () => signIn(false);
$("sign-up").onclick = () => signIn(true);
$("worker").onchange = async (event) => {
  await chrome.storage.local.set({ panelWorker: event.target.value });
  await render();
};
$("menu-button").onclick = () => {
  const menu = $("menu");
  menu.hidden = !menu.hidden;
  $("menu-button").setAttribute("aria-expanded", String(!menu.hidden));
};
document.addEventListener("click", (event) => {
  if (!event.target.closest("#menu, #menu-button")) $("menu").hidden = true;
});
$("pause").onclick = () => run("pause", { paused: !state.paused });
$("reload").onclick = () => run("refresh");
$("sign-out").onclick = () => run("sign-out");

// The framed page reports the chat it shows (mobile/src/shell/extension-panel.ts).
window.addEventListener("message", (event) => {
  if (event.source !== $("app").contentWindow || event.origin !== shown.origin) return;
  const data = event.data;
  if (data?.type !== EXTENSION_SESSION_MESSAGE || typeof data.sessionId !== "string" || !data.sessionId) return;
  ask("claim-session", { workerId: shown.worker, sessionId: data.sessionId }).catch(() => {});
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.type === "status-changed") render();
});

render();
