import { randomUUID } from "node:crypto";
import { editField, uploadField } from "./editors.js";
import { Session, raceWithTimeout } from "./session.js";
import { object, validateCommand, type BrowserCommand, type BrowserWorkspaceState } from "./workspace-protocol.js";

// Internal app-owned process correlation; standalone sessions keep random IDs.
const runtimeIdentities = new WeakMap<Session, string>();
export function setWorkspaceRuntimeIdentity(session: Session, env = process.env): void {
  if (env.AIS_BROWSER_MODE === "runtime" && env._AIS_BROWSER_RUNTIME_ID) {
    runtimeIdentities.set(session, env._AIS_BROWSER_RUNTIME_ID);
  }
}

export const HUMAN_LEASE_MS = 65000;
export interface CommandResult { id: string; ok: boolean; error?: string }
/** Page operations share one queue; an admitted Stop interrupts it. */
export class BrowserWorkspace {
  readonly runtimeId: string;
  private viewer: string | null = null;
  private pending: string | null = null;
  private lease = 0;
  private ownershipGeneration = 0;
  private stopped = false;
  private queued = 0;
  private seen = new Set<string>();
  private timer: ReturnType<typeof setInterval>;
  private revision = 0;
  private metadata: Pick<BrowserWorkspaceState, "target_id" | "active_tab_id" | "tabs" | "viewport"> = {
    target_id: null, active_tab_id: null, tabs: [], viewport: { mode: "fixed", width: 1280, height: 800 },
  };
  onChange?: () => void;
  constructor(readonly session: Session, runtimeId = runtimeIdentities.get(session) ?? randomUUID(), private now = Date.now) {
    this.runtimeId = runtimeId;
    session.agentAdmission = () => this.admitAgent();
    session.onWorkspaceChange = () => this.changed();
    session.workspaceStatus = () => ({ ...this.state() });
    this.timer = setInterval(() => this.expireLease(), 1000);
    this.timer.unref();
  }
  private changed(): void { this.revision++; this.onChange?.(); }
  expireLease(): void {
    if ((this.viewer || this.pending) && this.now() >= this.lease) this.clearControl();
  }
  private clearControl(): void {
    this.viewer = this.pending = null;
    this.ownershipGeneration++;
    this.lease = 0;
    this.changed();
  }
  admitAgent(): void {
    this.expireLease();
    if (this.stopped) throw new Error("browser workspace runtime stopped; reconnect the browser server");
    if (this.viewer || this.pending) throw new Error("human_control: browser is controlled by a viewer or takeover is pending. Use browser_status; retry after control is returned.");
  }
  state(): BrowserWorkspaceState {
    return { runtime_id: this.runtimeId, revision: this.revision, available: !this.stopped,
      ...this.metadata, control: { owner: this.viewer ? "human" : "agent", viewer_id: this.viewer, pending_viewer_id: this.pending }, busy: this.session.busy };
  }
  async refresh(): Promise<void> {
    const rev = this.revision;
    const metadata = await raceWithTimeout(this.session.workspaceMetadata(), 3000, "workspace metadata timeout");
    if (rev === this.revision) {
      if (JSON.stringify(metadata) !== JSON.stringify(this.metadata)) { this.metadata = metadata; this.changed(); }
    }
  }
  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    clearInterval(this.timer);
    this.clearControl();
  }
  async receive(raw: unknown): Promise<CommandResult> {
    let id = object(raw) && typeof raw.id === "string" && raw.id.length <= 256 ? raw.id : "invalid";
    try {
      const c = validateCommand(raw, this.runtimeId, this.now()); id = c.id;
      if (this.stopped) throw new Error("runtime stopped");
      if (this.seen.has(id)) throw new Error("duplicate command; not replayed");
      const interrupt = c.op === "command" && c.args.command === "stop";
      if (!interrupt && this.queued >= 64) throw new Error("workspace queue full");
      this.seen.add(id);
      if (this.seen.size > 1024) this.seen.delete(this.seen.values().next().value!);
      this.expireLease();
      if (c.op === "release" || c.op === "heartbeat") {
        if (c.viewer_id !== this.viewer && c.viewer_id !== this.pending) throw new Error("viewer does not own control");
        if (c.op === "release") this.clearControl(); else this.lease = this.now() + HUMAN_LEASE_MS;
        return { id, ok: true };
      }
      if (c.op === "takeover") {
        if ((this.viewer && this.viewer !== c.viewer_id) || (this.pending && this.pending !== c.viewer_id)) throw new Error("another viewer owns control");
        if (this.viewer === c.viewer_id) { this.lease = this.now() + HUMAN_LEASE_MS; return { id, ok: true }; }
        this.pending = c.viewer_id;
        this.lease = this.now() + HUMAN_LEASE_MS;
        this.changed(); // reserve BEFORE entering queue: queued agent calls recheck admission
      }
      const generation = this.ownershipGeneration;
      if (interrupt) {
        const check = () => {
          this.expireLease();
          validateCommand(c, this.runtimeId, this.now());
          if (this.stopped || generation !== this.ownershipGeneration) throw new Error("control reservation expired or released");
          if (this.viewer !== c.viewer_id) throw new Error("human control required");
          if (!c.tab_id || c.tab_id !== this.session.currentTabId()) throw new Error("stale tab: refresh workspace state");
        };
        check();
        const cdp = await this.session.currentCDP();
        check();
        await raceWithTimeout(cdp.send("Page.stopLoading"), 5000, "stop timed out");
        if (!this.stopped && c.tab_id === this.session.currentTabId()) this.session.notifyNavigation();
        return { id, ok: true };
      }
      this.queued++;
      try {
        await this.session.runExclusive(async () => {
          this.expireLease();
          validateCommand(c, this.runtimeId, this.now());
          if (this.stopped || generation !== this.ownershipGeneration) throw new Error("control reservation expired or released");
          if (c.op === "takeover") {
            if (this.viewer === c.viewer_id) return; // duplicate pending request settled by an earlier takeover
            if (this.pending !== c.viewer_id) throw new Error("takeover cancelled");
            this.viewer = c.viewer_id; this.pending = null;
            // A human at the wheel owns the viewport: drop the agent's pin so
            // panel resizes are not undone on the agent's next call.
            this.session.clearPinnedViewport();
            this.session.notifyNavigation(); this.changed(); return;
          }
          if (this.viewer !== c.viewer_id) throw new Error("human control required");
          const canBeNull = c.op === "tabs" && c.args.action === "create";
          if (!(canBeNull && c.tab_id === null) && (!c.tab_id || c.tab_id !== this.session.currentTabId())) throw new Error("stale tab: refresh workspace state");
          await this.execute(c);
        });
      } catch (err) {
        if (c.op === "takeover" && this.pending === c.viewer_id && generation === this.ownershipGeneration) this.clearControl();
        throw err;
      } finally { this.queued--; this.changed(); }
      return { id, ok: true };
    } catch (err) {
      // Protocol errors intentionally do not echo arbitrary CDP/page payloads.
      return { id, ok: false, error: (err instanceof Error ? err.message : "workspace command failed").slice(0, 1024) };
    }
  }
  private async execute(c: BrowserCommand): Promise<void> {
    const a = c.args;
    this.session.notifyNavigation();
    if (c.op === "tabs") {
      if (a.action === "create") {
        await this.session.createTab();
        if (a.url) await (await this.session.ensurePage()).goto(a.url as string, { waitUntil: "domcontentloaded", timeout: 15000 });
      } else if (a.action === "select") await this.session.selectTab(a.tab_id as string);
      else await this.session.closeTab(a.tab_id as string);
      return;
    }
    if (!this.session.hasPage()) throw new Error("no active page; create a tab first");
    const page = await this.session.ensurePage();
    if (c.op === "navigate") await page.goto(a.url as string, { waitUntil: "domcontentloaded", timeout: 15000 });
    else if (c.op === "viewport") {
      await this.session.setViewport(a.width as number, a.height as number);
      this.session.viewportMode = a.mode as "fit" | "fixed";
    } else if (c.op === "upload") {
      await uploadField(page, a);
    } else if (c.op === "edit") {
      await editField(page, a);
    } else if (c.op === "input") {
      const cdp = await this.session.currentCDP();
      await raceWithTimeout(cdp.send(a.method as "Input.insertText", a.params as { text: string }), 5000, "input timed out; outcome unknown, do not replay");
    } else if (c.op === "command") {
      const options = { waitUntil: "domcontentloaded" as const, timeout: 15000 };
      if (a.command === "back") await page.goBack({ ...options, waitUntil: "commit" });
      else if (a.command === "forward") await page.goForward({ ...options, waitUntil: "commit" });
      else if (a.command === "reload") await page.reload(options);
    }
  }
}
