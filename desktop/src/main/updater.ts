/** Source, dependencies and native runtimes share one in-app update/restart action. */
import { app } from 'electron';
import path from 'node:path';
import type { SourceUpdates } from './source-updates';

export type UpdatePhase = 'idle' | 'checking' | 'available' | 'downloading' | 'ready' | 'error' | 'unsupported';
export interface UpdateState {
  phase: UpdatePhase; version: string | null; percent: number | null;
  error: string | null; checkedAt: string | null; message?: string | null;
}
export class Updater {
  private state: UpdateState = { phase: 'unsupported', version: null, percent: null, error: null, checkedAt: null };
  private listeners = new Set<(state: UpdateState) => void>();
  private timer: ReturnType<typeof setInterval> | null = null;
  private initialCheck: ReturnType<typeof setTimeout> | null = null;
  private applications: SourceUpdates | null = null;
  private installedExecutable: string | undefined;
  private restartArgs: string[] = [];
  private checking = false;
  private downloading = false;
  private installing = false;
  private beforeInstall: () => Promise<void> = async () => {};
  constructor(private readonly releaseUpdates = true) {}
  onBeforeInstall(action: () => Promise<void>): void { this.beforeInstall = action; }
  useApplications(applications: SourceUpdates, installedExecutable?: string, restartArgs: string[] = []): void {
    if (!this.releaseUpdates || process.env.SHUFFLE_DESKTOP_NO_UPDATES === '1') return;
    this.applications = applications;
    this.installedExecutable = installedExecutable;
    this.restartArgs = restartArgs;
    const pending = applications.pendingVersion !== applications.version ? applications.pendingVersion : null;
    this.set({ phase: pending ? 'ready' : 'idle', version: pending?.slice(0, 12) ?? null, error: null, message: null, percent: null });
  }
  get current(): UpdateState { return { ...this.state }; }
  subscribe(fn: (state: UpdateState) => void): () => void { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  private set(patch: Partial<UpdateState>): void { this.state = { ...this.state, ...patch }; for (const fn of this.listeners) fn(this.current); }
  start(): void {
    if (!this.applications || this.timer) return;
    this.initialCheck = setTimeout(() => { this.initialCheck = null; void this.check(); }, 15_000);
    this.timer = setInterval(() => void this.check(), 10 * 60 * 1000);
  }
  stop(): void { if (this.initialCheck) clearTimeout(this.initialCheck); this.initialCheck = null; if (this.timer) clearInterval(this.timer); this.timer = null; }
  async check(interactive = false): Promise<void> {
    if (!this.applications || this.checking || this.downloading || this.installing || this.state.phase === 'ready') return;
    this.checking = true; this.set({ phase: 'checking', error: null, percent: null, message: 'Checking main for updates…' });
    try {
      const result = await this.applications.check(undefined, interactive);
      this.set({ phase: result.kind === 'current' ? 'idle' : result.kind, version: result.version ?? null, percent: null, message: null, checkedAt: new Date().toISOString() });
    } catch (error) { this.set({ phase: 'error', error: String(error), message: null }); }
    finally { this.checking = false; }
  }
  async download(): Promise<void> {
    if (!this.applications || this.checking || this.downloading || this.installing || !['available', 'error'].includes(this.state.phase)) return;
    this.downloading = true; this.set({ phase: 'downloading', error: null, percent: null, message: 'Preparing update…' });
    try {
      const result = await this.applications.download((percent, message) => this.set({ percent, message }), true);
      this.set({ phase: result.kind === 'current' ? 'idle' : result.kind, version: result.version ?? null, percent: null, message: null });
    } catch (error) { this.set({ phase: 'error', error: String(error), percent: null, message: null }); }
    finally { this.downloading = false; }
  }
  async install(): Promise<void> {
    if (!this.applications || this.state.phase !== 'ready' || this.checking || this.downloading || this.installing) return;
    this.installing = true; this.set({ error: null, message: null });
    try {
      await this.beforeInstall();
      if (this.installedExecutable) {
        app.relaunch({ execPath: this.installedExecutable, args: this.restartArgs });
        app.quit();
        return;
      }
      const current = this.applications.version ? await this.applications.application(this.applications.version) : null;
      if (!current) throw new Error('No running source-managed application');
      // Relaunch through Node, not the currently running Electron executable.
      // The bootstrap selects the staged source AND its matching native runtimes.
      app.relaunch({ execPath: current.node, args: [path.join(current.desktopDir, 'scripts/bootstrap.mjs'), '--root', this.applications.options.root] });
      app.quit();
    } catch (error) { this.installing = false; this.set({ phase: 'ready', error: String(error), message: null }); }
  }
}
