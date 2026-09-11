import { BrowserWorkspace, type CommandResult } from "./workspace.js";
import { Session } from "./session.js";
import { disableLegacyReports } from "./panel.js";
import { object } from "./workspace-protocol.js";

export interface BridgeConfig { sessionId: string; token: string; baseUrl: string }
export function bridgeConfig(env = process.env): BridgeConfig | undefined {
  if (!env.AIS_SESSION_ID || !env.AIS_PANEL_TOKEN || !env.AIS_PANEL_REGISTER_URL) return;
  const url = new URL(env.AIS_PANEL_REGISTER_URL);
  url.pathname = url.pathname.replace(/\/register\/?$/, "/control/");
  url.search = ""; url.hash = "";
  return { sessionId: env.AIS_SESSION_ID, token: env.AIS_PANEL_TOKEN, baseUrl: url.href };
}
class Superseded extends Error {}
/** Separate poll and report lanes: completion never waits for a long poll. */
export class WorkspaceBridge {
  readonly workspace: BrowserWorkspace;
  private abort = new AbortController();
  private registered = false;
  private started = false;
  private dirty = true;
  private reporting = false;
  private results: CommandResult[] = [];
  private refreshTimer?: ReturnType<typeof setInterval>;
  private refreshing = false;
  private refreshSoon?: ReturnType<typeof setTimeout>;
  private lastRefresh = 0;
  constructor(session: Session, private config: BridgeConfig, private transport: typeof fetch = fetch) {
    this.workspace = new BrowserWorkspace(session);
    this.workspace.onChange = () => { this.dirty = true; void this.flush(); void this.refresh(); };
  }
  start(): void {
    if (this.started || this.abort.signal.aborted) return;
    this.started = true;
    disableLegacyReports();
    this.refreshTimer = setInterval(() => void this.refresh(), 500);
    this.refreshTimer.unref();
    void this.pollLoop();
  }
  async shutdown(): Promise<void> {
    if (this.abort.signal.aborted) return;
    this.workspace.stop();
    try {
      if (this.registered) await this.post("report", { state: this.workspace.state(), results: this.results.slice(0, 64) }, 1000);
    } catch { /* shutdown is best effort; heartbeat expiry is the fallback */ }
    finally { this.stop(); }
  }
  stop(): void {
    if (this.abort.signal.aborted) return;
    this.abort.abort();
    clearInterval(this.refreshTimer);
    clearTimeout(this.refreshSoon);
    this.workspace.stop();
    this.results = [];
  }
  private async pause(ms: number): Promise<void> {
    if (this.abort.signal.aborted) return;
    await new Promise<void>(resolve => {
      const done = () => { clearTimeout(timer); this.abort.signal.removeEventListener("abort", done); resolve(); };
      const timer = setTimeout(done, ms);
      this.abort.signal.addEventListener("abort", done, { once: true });
    });
  }
  private async post(endpoint: string, data: object, timeout: number): Promise<unknown> {
    const response = await this.transport(new URL(endpoint, this.config.baseUrl), {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ session_id: this.config.sessionId, token: this.config.token, runtime_id: this.workspace.runtimeId, ...data }),
      signal: AbortSignal.any([this.abort.signal, AbortSignal.timeout(timeout)]),
      redirect: "error",
    });
    if (response.status === 409) { this.stop(); throw new Superseded(); }
    if (response.status === 401 || response.status === 403) { this.stop(); throw new Error("workspace authentication rejected"); }
    if (!response.ok) { await response.body?.cancel(); throw new Error("workspace transport unavailable"); }
    if (response.status === 204) return null;
    const reader = response.body?.getReader();
    if (!reader) return null;
    let size = 0; const chunks: Uint8Array[] = [];
    try {
      while (true) {
        const { done, value } = await reader.read(); if (done) break;
        size += value.length;
        if (size > 262144) throw new Error("workspace response too large");
        chunks.push(value);
      }
      return JSON.parse(Buffer.concat(chunks).toString("utf8"));
    } finally { await reader.cancel().catch(() => {}); }
  }
  private async refresh(): Promise<void> {
    if (this.refreshing || this.abort.signal.aborted) return;
    if (Date.now() - this.lastRefresh < 200) {
      if (!this.refreshSoon) this.refreshSoon = setTimeout(() => { this.refreshSoon = undefined; void this.refresh(); }, 200);
      return;
    }
    this.lastRefresh = Date.now();
    this.refreshing = true;
    try { await this.workspace.refresh(); } catch { /* next heartbeat retries metadata */ }
    finally { this.refreshing = false; }
  }
  private async pollLoop(): Promise<void> {
    let delay = 250;
    while (!this.abort.signal.aborted) {
      try {
        if (!this.registered) {
          await this.post("register", {}, 5000); this.registered = true;
          void this.flush(); void this.refresh();
        }
        const body = await this.post("poll", {}, 25000);
        if (!object(body) || !Array.isArray(body.commands) || body.commands.length > 16) throw new Error("invalid poll response");
        for (const command of body.commands) {
          // receive reserves takeover synchronously, before any promise continuation.
          void this.workspace.receive(command).then(result => {
            if (this.abort.signal.aborted) return;
            if (this.results.length >= 512) { this.stop(); return; }
            this.results.push(result); void this.flush(); void this.refresh();
          });
        }
        delay = 250;
        if (!body.commands.length) await this.pause(50); // protect against broken immediate-empty servers
      } catch {
        if (this.abort.signal.aborted) return;
        await this.pause(delay); delay = Math.min(delay * 2, 5000);
      }
    }
  }
  private async flush(): Promise<void> {
    if (!this.registered || this.reporting || this.abort.signal.aborted) return;
    this.reporting = true;
    try {
      while (!this.abort.signal.aborted && (this.dirty || this.results.length)) {
        this.dirty = false;
        const results = this.results.slice(0, 64);
        try {
          await this.post("report", { state: this.workspace.state(), results }, 5000);
          this.results.splice(0, results.length);
        } catch {
          if (this.abort.signal.aborted) return;
          this.dirty = true; await this.pause(1000);
        }
        // Coalesce mouse movement / bursts, without holding a result behind polling.
        if (this.dirty && !this.results.length) await this.pause(50);
      }
    } finally { this.reporting = false; }
  }
}
export function startWorkspaceBridge(session: Session): WorkspaceBridge | undefined {
  const config = bridgeConfig();
  if (!config) return;
  const bridge = new WorkspaceBridge(session, config); bridge.start(); return bridge;
}
