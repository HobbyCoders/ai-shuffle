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
/** The server refused this body (or it is too large to send): resending it unchanged cannot succeed. */
class Rejected extends Error { constructor(message: string, readonly status: number) { super(message); } }
/** The server's request body limit (browser_control.MAX_BODY). */
export const MAX_REPORT_BODY = 262144;
/** Separate poll and report lanes: completion never waits for a long poll. */
export class WorkspaceBridge {
  readonly workspace: BrowserWorkspace;
  private abort = new AbortController();
  private registered = false;
  private started = false;
  private dirty = true;
  private reporting = false;
  private results: CommandResult[] = [];
  private refreshTimer?: ReturnType<typeof setTimeout>;
  private viewerCount?: number;
  private refreshing = false;
  private refreshRequested = false;
  private refreshSoon?: ReturnType<typeof setTimeout>;
  private lastRefresh = 0;
  private lastActivity = Date.now();
  constructor(session: Session, private config: BridgeConfig, private transport: typeof fetch = fetch) {
    this.workspace = new BrowserWorkspace(session);
    this.workspace.onChange = () => { this.dirty = true; void this.flush(); };
    const workspaceChanged = session.onWorkspaceChange!;
    session.onWorkspaceChange = () => { workspaceChanged(); void this.refresh(); };
  }
  start(): void {
    if (this.started || this.abort.signal.aborted) return;
    this.started = true;
    disableLegacyReports();
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
    clearTimeout(this.refreshTimer);
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
    const body = JSON.stringify({ session_id: this.config.sessionId, token: this.config.token, runtime_id: this.workspace.runtimeId, ...data });
    if (Buffer.byteLength(body) > MAX_REPORT_BODY) throw new Rejected("workspace payload too large", 413);
    const response = await this.transport(new URL(endpoint, this.config.baseUrl), {
      method: "POST", headers: { "content-type": "application/json", origin: new URL(this.config.baseUrl).origin },
      body,
      signal: AbortSignal.any([this.abort.signal, AbortSignal.timeout(timeout)]),
      redirect: "error",
    });
    if (response.status === 409) { this.stop(); throw new Superseded(); }
    if (response.status === 401 || response.status === 403) { this.stop(); throw new Error("workspace authentication rejected"); }
    if (!response.ok) {
      await response.body?.cancel();
      // Only a definite refusal of this body: too large (413), or invalid (422).
      // Other 4xx (a proxy's 408 or 429, say) are transient and retried as-is;
      // flush() decides whether a 422 can be blamed on a payload.
      throw response.status === 413 || response.status === 422 ? new Rejected(`workspace payload rejected (${response.status})`, response.status) : new Error("workspace transport unavailable");
    }
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
  private updateViewers(body: unknown): void {
    const viewers = object(body) && typeof body.viewer_count === "number" && Number.isSafeInteger(body.viewer_count) && body.viewer_count >= 0 ? body.viewer_count : undefined;
    if (viewers === this.viewerCount) return;
    this.viewerCount = viewers;
    this.scheduleRefresh();
    if (viewers !== 0) void this.refresh();
  }
  private scheduleRefresh(): void {
    clearTimeout(this.refreshTimer);
    if (this.abort.signal.aborted || this.viewerCount === 0) return;
    // Events and commands refresh immediately. A passive viewer needs only a
    // slower fallback for DOM/title changes that Playwright does not report.
    // Heartbeats preserve ownership, but must not keep metadata collection hot.
    const active = this.viewerCount !== undefined && Date.now() - this.lastActivity < 5000;
    this.refreshTimer = setTimeout(() => void this.refresh(false), active ? 1000 : 5000);
    this.refreshTimer.unref();
  }
  private async refresh(activity = true): Promise<void> {
    if (this.abort.signal.aborted) return;
    if (activity) this.lastActivity = Date.now();
    if (this.refreshing) { this.refreshRequested = true; return; }
    if (Date.now() - this.lastRefresh < 200) {
      if (!this.refreshSoon) this.refreshSoon = setTimeout(() => { this.refreshSoon = undefined; void this.refresh(false); }, 200);
      return;
    }
    this.lastRefresh = Date.now();
    this.refreshing = true;
    try { await this.workspace.refresh(); } catch { /* the next page event or viewer refresh retries metadata */ }
    finally {
      this.refreshing = false;
      this.scheduleRefresh();
      if (this.refreshRequested) { this.refreshRequested = false; void this.refresh(false); }
    }
  }
  private async pollLoop(): Promise<void> {
    let delay = 250;
    while (!this.abort.signal.aborted) {
      try {
        if (!this.registered) {
          const registration = await this.post("register", {}, 5000); this.registered = true;
          this.updateViewers(registration);
          void this.flush(); void this.refresh();
        }
        const body = await this.post("poll", {}, 25000);
        if (!object(body) || !Array.isArray(body.commands) || body.commands.length > 16) throw new Error("invalid poll response");
        this.updateViewers(body);
        for (const command of body.commands) {
          // receive reserves takeover synchronously, before any promise continuation.
          void this.workspace.receive(command).then(result => {
            if (this.abort.signal.aborted) return;
            if (this.results.length >= 512) { this.stop(); return; }
            this.results.push(result); void this.flush();
            if (!object(command) || command.op !== "heartbeat") void this.refresh();
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
        const results = reportBatch(this.results);
        try {
          await this.post("report", { state: this.workspace.state(), results }, 5000);
          this.results.splice(0, results.length);
        } catch (error) {
          if (this.abort.signal.aborted) return;
          // A copy or option list the server refuses (too long, too large) would
          // be resent forever and hold every later result and state behind it:
          // its viewer gets an error instead.
          // A 422 with no payload in the batch points at the state, not a list or copy.
          if (error instanceof Rejected && (error.status === 413 || results.some(hasPayload)) && withoutPayloads(this.results, results.length)) continue;
          this.dirty = true; await this.pause(1000);
        }
        // Coalesce mouse movement / bursts, without holding a result behind polling.
        if (this.dirty && !this.results.length) await this.pause(50);
      }
    } finally { this.reporting = false; }
  }
}
/**
 * The next results to report: up to 64, but at most one that carries a
 * payload (a copy's text or a list's options), so a single report stays well
 * inside the server's body limit.
 */
export function reportBatch(results: readonly CommandResult[]): CommandResult[] {
  const batch = results.slice(0, 64);
  const second = batch.findIndex((r, i) => i > 0 && (r.text !== undefined || r.choices !== undefined));
  return second > 0 ? batch.slice(0, second) : batch;
}
const hasPayload = (r: CommandResult): boolean => r.text !== undefined || r.choices !== undefined;
/**
 * Replaces the first `count` results' payloads with an error for their
 * viewer. Returns whether there was a payload to drop.
 */
export function withoutPayloads(results: CommandResult[], count: number): boolean {
  let dropped = false;
  for (let i = 0; i < count && i < results.length; i++) {
    const r = results[i];
    if (!hasPayload(r)) continue;
    results[i] = { id: r.id, ok: false, error: r.choices !== undefined ? "This list is too long to show here." : "That's too much text to copy at once." };
    dropped = true;
  }
  return dropped;
}
export function startWorkspaceBridge(session: Session): WorkspaceBridge | undefined {
  const config = bridgeConfig();
  if (!config) return;
  const bridge = new WorkspaceBridge(session, config); bridge.start(); return bridge;
}
