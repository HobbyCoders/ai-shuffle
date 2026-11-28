// Account sign-in and worker enrollment, the same flow the Android app uses:
// the account service (hobbycoders.com) signs the person in through Clerk with
// PKCE, then mints a one-minute ticket per worker, and each worker exchanges
// that ticket for a device key scoped to "browser:extension" only.

export const HUB = "https://hobbycoders.com";

const b64url = (bytes) =>
  btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const randomToken = (n = 32) => b64url(crypto.getRandomValues(new Uint8Array(n)));
const sha256 = async (text) => crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));

export class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function call(origin, path, { method = "GET", body, token } = {}) {
  let response;
  try {
    response = await fetch(origin + path, {
      method,
      headers: {
        ...(body ? { "Content-Type": "application/json" } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
      credentials: "omit",
      redirect: "error",
    });
  } catch {
    throw new HttpError(0, `Could not reach ${new URL(origin).host}`);
  }
  let data = {};
  try {
    data = await response.json();
  } catch {
    /* non-JSON error page */
  }
  if (!response.ok) {
    const detail = typeof data.error === "string" ? data.error : typeof data.detail === "string" ? data.detail : "";
    throw new HttpError(response.status, detail || `Request failed (${response.status})`);
  }
  return data;
}

/** This browser's stable identity: one UUID for the account and every worker. */
export async function identity() {
  let { identity } = await chrome.storage.local.get("identity");
  if (!identity) {
    identity = { device_id: crypto.randomUUID(), install_secret: randomToken() };
    await chrome.storage.local.set({ identity });
  }
  return identity;
}

export function browserInfo() {
  const brands = navigator.userAgentData?.brands || [];
  const edge = brands.find((b) => /Edge/i.test(b.brand));
  const chrome_ = brands.find((b) => /Google Chrome|Chromium/i.test(b.brand));
  const product = edge ? "Edge" : "Chrome";
  const version = (edge || chrome_)?.version || (navigator.userAgent.match(/Chrome\/([\d.]+)/) || [])[1] || "";
  const platform = navigator.userAgentData?.platform || (/Mac/.test(navigator.platform) ? "macOS" : "Windows");
  return { product, version, name: `${product} on ${platform}` };
}

/** Sign in through the account service in a browser window (chrome.identity). */
export async function signIn({ createAccount = false } = {}) {
  const { device_id } = await identity();
  const verifier = randomToken(48);
  const challenge = b64url(await sha256(verifier));
  const start = await call(HUB, "/v1/oauth/start", {
    method: "POST",
    body: {
      device_id,
      device_name: browserInfo().name,
      platform: "extension",
      extension_id: chrome.runtime.id,
      code_challenge: challenge,
    },
  });
  const redirect = chrome.identity.getRedirectURL();
  if (start.redirect_uri !== redirect) throw new Error("The account service does not accept this extension yet.");
  const state = randomToken(24);
  const url = new URL(HUB + "/connect");
  url.search = new URLSearchParams({
    client_id: start.client_id,
    redirect_uri: redirect,
    response_type: "code",
    scope: "openid profile email",
    state,
    nonce: start.nonce,
    code_challenge: challenge,
    code_challenge_method: "S256",
    request_id: start.request_id,
    screen: createAccount ? "signup" : "signin",
  }).toString();
  const returned = await chrome.identity.launchWebAuthFlow({ url: url.toString(), interactive: true });
  const back = new URL(returned);
  if (back.origin + "/" !== redirect) throw new Error("Sign-in returned to an unexpected address.");
  if (back.searchParams.get("error")) {
    throw new Error(back.searchParams.get("error_description") || "Sign-in was cancelled.");
  }
  if (back.searchParams.get("state") !== state) throw new Error("Sign-in could not be verified. Try again.");
  const code = back.searchParams.get("code");
  if (!code) throw new Error("Sign-in did not return an authorization code.");
  const session = await call(HUB, "/v1/oauth/exchange", {
    method: "POST",
    body: { request_id: start.request_id, code_verifier: verifier, code },
  });
  if (session.device_id !== device_id) throw new Error("The account service returned the wrong device.");
  await chrome.storage.local.set({
    session: { token: session.access_token, expires_at: session.expires_at, username: session.account?.username || "" },
  });
  return session;
}

export async function session() {
  const { session } = await chrome.storage.local.get("session");
  if (!session || Date.parse(session.expires_at) <= Date.now()) return null;
  return session;
}

export async function signOut() {
  await chrome.storage.local.remove(["session", "workers", "panelWorker"]);
  await chrome.storage.session.remove("webSessions");
}

async function installProof(serverId) {
  const { install_secret } = await identity();
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(install_secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return b64url(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(serverId)));
}

function workerOrigin(url) {
  const u = new URL(url);
  if (u.protocol !== "https:") throw new Error("Workers must use HTTPS");
  return u.origin;
}

/** Enroll with one worker through the account (fresh ticket each time). */
export async function enroll(server) {
  const current = await session();
  if (!current) throw new HttpError(401, "Sign in again");
  const { device_id } = await identity();
  const { ticket } = await call(HUB, `/v1/servers/${encodeURIComponent(server.id)}/tickets`, {
    method: "POST",
    token: current.token,
  });
  const origin = workerOrigin(server.url);
  const enrolled = await call(origin, "/api/v1/mobile/hub/enroll", {
    method: "POST",
    body: {
      ticket,
      device_info: {
        device_id,
        device_name: browserInfo().name,
        platform: "extension",
        capabilities: { supports_push: false },
      },
      install_proof: await installProof(server.id),
    },
  });
  if (enrolled.device_id !== device_id || !String(enrolled.api_key || "").startsWith("aim_")) {
    throw new Error(`${server.name} returned an unsupported answer. Update that worker.`);
  }
  return { id: server.id, name: server.name, origin, key: enrolled.api_key, expires_at: enrolled.expires_at };
}

/** The account's workers, enrolled as needed. Failures are kept per worker. */
export async function syncWorkers() {
  const current = await session();
  if (!current) throw new HttpError(401, "Sign in again");
  const { servers } = await call(HUB, "/v1/servers", { token: current.token });
  const { workers: saved = {} } = await chrome.storage.local.get("workers");
  const next = {};
  for (const server of servers || []) {
    const known = saved[server.id];
    const usable =
      known?.key && known.origin === safeOrigin(server.url) && Date.parse(known.expires_at) > Date.now() + 86400000;
    next[server.id] = usable ? { ...known, name: server.name } : { id: server.id, name: server.name, url: server.url };
    if (!usable) {
      try {
        next[server.id] = await enroll(server);
      } catch (error) {
        next[server.id].error = error.message;
      }
    }
    next[server.id].url = server.url;
  }
  await chrome.storage.local.set({ workers: next });
  return next;
}

function safeOrigin(url) {
  try {
    return workerOrigin(url);
  } catch {
    return null;
  }
}
