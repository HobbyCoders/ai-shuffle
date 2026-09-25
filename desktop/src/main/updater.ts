/** Source, dependencies and native runtimes share one in-app update/restart action. */
import { app } from 'electron';
import path from 'node:path';
import type { SourceUpdates } from './source-updates';
import type { InstallerUpdates } from './installer-updates';

export type UpdatePhase = 'idle' | 'checking' | 'available' | 'downloading' | 'ready' | 'error' | 'unsupported';
export interface UpdateState {
  phase: UpdatePhase; version: string | null; percent: number | null;
  error: string | null; checkedAt: string | null; message?: string | null;
  running_version?: string | null;
  /** Release name and notes from the signed manifest of the offered update. */
  release?: string | null; notes?: string | null;
}
/**
 * How often the shell trades update state with its worker (the web UI's
 * update button reads that state and queues check/download/install requests
 * there). Each exchange is a local request and a settings read in the
 * worker, so this is not the 2 s it used to be. It cannot go past about 8 s
 * without a worker change: the worker treats the desktop updater as gone
 * after 10 s of silence and drops a queued request after 10 s
 * (app/core/application_updates.py). Phase changes are pushed at once, so
 * the button still follows a check or download without waiting for a tick.
 */
export const BRIDGE_INTERVAL_MS = 8_000;

export class Updater {
  private state: UpdateState = { phase: 'unsupported', version: null, percent: null, error: null, checkedAt: null };
  private listeners = new Set<(state: UpdateState) => void>();
  private timer: ReturnType<typeof setInterval> | null = null;
  private initialCheck: ReturnType<typeof setTimeout> | null = null;
  private applications: SourceUpdates | null = null;
  /** macOS and Linux: new releases arrive as installers (installer-updates.ts); no in-place install. */
  private installers: Pick<InstallerUpdates, 'check' | 'download'> | null = null;
  private saved: (file: string) => void = () => {};
  private installedExecutable: string | undefined;
  private restartArgs: string[] = [];
  private checking = false;
  private downloading = false;
  private installing = false;
  private beforeInstall: () => Promise<void> = async () => {};
  private installFailed: (error: unknown) => Promise<void> = async () => {};
  private policy: () => Promise<boolean> = async () => true;
  private bridge: ((state: UpdateState) => Promise<Record<string, unknown>>) | null = null;
  private safeToRestart: () => Promise<boolean> = async () => false;
  private bridgeTimer: ReturnType<typeof setInterval> | null = null;
  private exchanging = false;
  private exchangeAgain = false;
  setBridge(exchange: (state: UpdateState) => Promise<Record<string, unknown>>, safeToRestart: () => Promise<boolean>): void {
    this.bridge = exchange; this.safeToRestart = safeToRestart;
  }
  async exchange(): Promise<void> {
    if (!this.bridge) return;
    if (this.exchanging) { this.exchangeAgain = true; return; }
    this.exchanging = true;
    try {
      const request = await this.bridge(this.current);
      if (!this.bridgeTimer || typeof request.expires_at !== 'number' || request.expires_at * 1000 < Date.now()) return;
      if (request.action === 'check') void this.check();
      else if (request.action === 'download') void this.download();
      else if (request.action === 'install') {
        if (await this.safeToRestart()) void this.install();
        else this.set({ error: 'Busy with active sessions; retry when finished.' });
      }
    } catch { /* Worker startup/restart is not an update failure. Retry the heartbeat, not the command. */ }
    finally {
      // A phase change during the exchange: report it now rather than on the next tick.
      const again = this.exchangeAgain && this.bridgeTimer !== null;
      this.exchanging = false; this.exchangeAgain = false;
      if (again) void this.exchange();
    }
  }
  constructor(private readonly releaseUpdates = true) {}
  setPolicy(policy: () => Promise<boolean>): void { this.policy = policy; }
  private async permitted(): Promise<boolean> {
    try {
      if (await this.policy()) return true;
      this.set({ error: null, message: 'Application updates are disabled in Account settings or the local worker is not ready.' });
    } catch {
      this.set({ error: 'Could not read the account update setting. No update was applied.' });
    }
    return false;
  }
  onBeforeInstall(action: () => Promise<void>): void { this.beforeInstall = action; }
  /** Runs when an install fails after onBeforeInstall began, so the owner can restart what it stopped. */
  onInstallFailed(action: (error: unknown) => Promise<void>): void { this.installFailed = action; }
  useApplications(applications: SourceUpdates, installedExecutable?: string, restartArgs: string[] = []): void {
    if (!this.releaseUpdates || process.env.SHUFFLE_DESKTOP_NO_UPDATES === '1') return;
    this.applications = applications;
    applications.stageDownloads?.();
    this.installedExecutable = installedExecutable;
    this.restartArgs = restartArgs;
    const pending = applications.pendingVersion !== applications.version ? applications.pendingVersion : null;
    this.set({ phase: pending ? 'ready' : 'idle', version: pending?.slice(0, 12) ?? null, error: null, message: null, percent: null, release: null, notes: null });
    // Older source managers have no releaseInfo; the prompt then shows no notes.
    if (pending) void applications.releaseInfo?.(pending).then(info => {
      if (this.state.phase === 'ready' && this.state.version === pending.slice(0, 12)) this.set({ release: info.release ?? null, notes: info.notes ?? null });
    }).catch(() => {});
  }
  /**
   * macOS and Linux: check the signed installer list on launch and daily (D22,
   * no consent prompt) and offer newer installers. Download saves and verifies
   * the installer and hands its path to saved(); the owner runs it. Windows
   * uses useApplications instead, and a source-managed app never uses both.
   */
  useInstallers(installers: Pick<InstallerUpdates, 'check' | 'download'>, saved: (file: string) => void): void {
    if (!this.releaseUpdates || process.env.SHUFFLE_DESKTOP_NO_UPDATES === '1' || this.applications) return;
    this.installers = installers; this.saved = saved;
    this.set({ phase: 'idle', version: null, error: null, message: null, percent: null, release: null, notes: null });
  }
  get current(): UpdateState { return { ...this.state, running_version: this.applications?.version ?? null }; }
  subscribe(fn: (state: UpdateState) => void): () => void { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  private set(patch: Partial<UpdateState>): void {
    const phase = this.state.phase;
    this.state = { ...this.state, ...patch };
    for (const fn of this.listeners) fn(this.current);
    // Progress ticks wait for the interval; a new phase (checking, available, ready, ...) goes out now.
    if (this.state.phase !== phase && this.bridgeTimer) void this.exchange();
  }
  start(): void {
    if (this.timer || this.bridgeTimer) return;
    this.stop();
    if (this.bridge) {
      this.bridgeTimer = setInterval(() => void this.exchange(), BRIDGE_INTERVAL_MS);
      void this.exchange();
    }
    if ((!this.applications && !this.installers) || this.timer) return;
    this.initialCheck = setTimeout(() => { this.initialCheck = null; void this.check(); }, 15_000);
    // Once on launch (above), then daily; users can still check from the app.
    this.timer = setInterval(() => void this.check(), 24 * 60 * 60 * 1000);
  }
  stop(): void { if (this.bridgeTimer) clearInterval(this.bridgeTimer); this.bridgeTimer = null; if (this.initialCheck) clearTimeout(this.initialCheck); this.initialCheck = null; if (this.timer) clearInterval(this.timer); this.timer = null; }
  async check(interactive = false): Promise<void> {
    if ((!this.applications && !this.installers) || this.checking || this.downloading || this.installing || this.state.phase === 'ready') return;
    this.checking = true; this.set({ phase: 'checking', error: null, percent: null, message: 'Checking for updates…' });
    try {
      if (!await this.permitted()) { this.set({ phase: 'idle' }); return; }
      if (!this.applications) {
        const found = await this.installers!.check();
        this.set(found.kind === 'available'
          ? { phase: 'available', version: found.version, percent: null, message: `AI Shuffle ${found.version} is available. Download the installer to update.`, checkedAt: new Date().toISOString(), release: found.release, notes: null }
          : { phase: 'idle', version: null, percent: null, message: null, checkedAt: new Date().toISOString(), release: null, notes: null });
        return;
      }
      const result = await this.applications.check(undefined, interactive);
      this.set({ phase: result.kind === 'current' ? 'idle' : result.kind, version: result.version ?? null, percent: null, message: null, checkedAt: new Date().toISOString(), release: result.release ?? null, notes: result.notes ?? null });
    } catch (error) { this.set({ phase: 'error', error: String(error), message: null }); }
    finally { this.checking = false; }
  }
  async download(): Promise<void> {
    if ((!this.applications && !this.installers) || this.checking || this.downloading || this.installing || !['available', 'error'].includes(this.state.phase)) return;
    this.downloading = true; this.set({ phase: 'downloading', error: null, percent: null, message: 'Preparing update…' });
    try {
      if (!await this.permitted()) { this.set({ phase: 'available' }); return; }
      if (!this.applications) {
        const { file, version } = await this.installers!.download((percent, message) => this.set({ percent, message }));
        this.set({ phase: 'idle', version: null, percent: null, error: null, release: null, notes: null, message: `AI Shuffle ${version} was downloaded and verified. Open ${file} to install it.` });
        this.saved(file);
        return;
      }
      const result = await this.applications.download((percent, message) => this.set({ percent, message }), true);
      this.set({ phase: result.kind === 'current' ? 'idle' : result.kind, version: result.version ?? null, percent: null, message: null, release: result.release ?? null, notes: result.notes ?? null });
    } catch (error) { this.set({ phase: 'error', error: String(error), percent: null, message: null }); }
    finally { this.downloading = false; }
  }
  async install(): Promise<void> {
    if (!this.applications || this.state.phase !== 'ready' || this.checking || this.downloading || this.installing) return;
    this.installing = true; this.set({ error: null, message: null });
    let stopping = false;
    try {
      if (!await this.permitted()) { this.installing = false; return; }
      stopping = true;
      await this.beforeInstall();
      await this.applications.prepareInstall?.();
      if (this.installedExecutable) {
        // Electron's relaunch inherits this process's environment. An older
        // installed launcher treats these variables as an instruction to attach
        // to the running revision, bypassing selection of the pending update.
        const launchKeys = ['SHUFFLE_SOURCE_ROOT', 'SHUFFLE_SOURCE_REVISION', 'SHUFFLE_SOURCE_NODE'] as const;
        const saved = launchKeys.map(key => process.env[key]);
        for (const key of launchKeys) delete process.env[key];
        try {
          app.relaunch({ execPath: this.installedExecutable, args: this.restartArgs });
          app.quit();
        } catch (error) {
          launchKeys.forEach((key, index) => { if (saved[index] !== undefined) process.env[key] = saved[index]; });
          throw error;
        }
        return;
      }
      const current = this.applications.version ? await this.applications.application(this.applications.version) : null;
      if (!current) throw new Error('No running source-managed application');
      // Relaunch through Node, not the currently running Electron executable.
      // The bootstrap selects the staged source AND its matching native runtimes.
      app.relaunch({ execPath: current.node, args: [path.join(current.desktopDir, 'scripts/bootstrap.mjs'), '--root', this.applications.options.root] });
      app.quit();
    } catch (error) {
      try { await this.applications.cancelInstall?.(); } catch (failure) { error = failure; }
      this.set({ phase: 'ready', error: String(error), message: null });
      if (stopping) {
        try { await this.installFailed(error); }
        catch (failure) { this.set({ error: `${String(error)}\nRestart failed: ${String(failure)}` }); }
      }
      this.installing = false;
    }
  }
}
