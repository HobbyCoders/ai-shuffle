/** Source, dependencies and native runtimes share one in-app update/restart action. */
import { app } from 'electron';
import path from 'node:path';
import type { SourceUpdates } from './source-updates';
import { installedRelease, returnToStable, type InstalledRelease } from './channel-return';
import type { InstallerUpdates } from './installer-updates';
import { timedPhase } from './timed-phase';

export type UpdatePhase = 'idle' | 'checking' | 'available' | 'downloading' | 'ready' | 'error' | 'unsupported';
export interface UpdateState {
  phase: UpdatePhase; version: string | null; percent: number | null;
  error: string | null; checkedAt: string | null; message?: string | null;
  running_version?: string | null;
  staged_version?: string | null;
  channel_config?: { channel: 'stable' | 'nightly'; source: 'file' | 'default'; editable: boolean };
  /** What this install runs: lets the worker check a return from nightly to stable. */
  installed_release?: InstalledRelease;
  /** Why the last return to stable was refused, and which attempt; sent once. */
  channel_error?: string;
  channel_attempt?: number;
  /** Release name and notes from the signed manifest of the offered update. */
  release?: string | null; notes?: string | null;
  /** macOS and Linux: the verified installer just saved, for the web UI to announce. */
  installer?: string | null;
  /**
   * After a failed first start that cannot be retried: whether the hub's
   * installer is a newer release than the one running (null: unknown). The
   * worker offers the installer for that failure only when it is.
   */
  installer_newer?: boolean | null;
  /** The offered update is one whose last first start did not finish and was put back (RETRY_MESSAGE says so). */
  retry_start?: boolean;
}
/** The source updater's refusal of a revision whose first start failed (source-updates.ts update()). */
export const FAILED_START = /this revision failed to start/i;
/** Said beside an update offered again after its first start did not finish and was put back. */
export const RETRY_MESSAGE = 'The last attempt to start this update did not finish, and AI Shuffle went back to the previous version with your data as it was. Download it to try again. The first start after an update can take a few minutes.';
/** Said when an install failed after the database copy for it was made and that copy could not be removed. */
export const KEPT_POINT_MESSAGE = 'The update stays ready to start, with the copy of your database made for it. Quit AI Shuffle and open it again to finish installing it; until then this version does not reopen the database.';
/** Said while the app restarts into an update; the new version's window can take minutes to appear. */
export const RESTARTING_MESSAGE = 'Starting the new version. This can take a few minutes; AI Shuffle opens again by itself.';
/** Recovery for an update whose first start did not finish (failed-start.ts). */
export interface FailedStartRecovery {
  /** Make every fully put-back failed revision offerable again and reload the source updater; the revisions released. */
  release(): Promise<string[]>;
  /** Whether the hub's installer is newer than the running release; null when unknown. */
  installerNewer(): Promise<boolean | null>;
}
/**
 * How often the shell trades update state with its worker (the web UI's
 * update button reads that state and queues check/download/install requests
 * there). Each exchange is a local request; the worker answers one with
 * nothing queued without a settings read. The worker treats the desktop
 * updater as present for 75 s after a report and keeps a queued request for
 * 90 s (DESKTOP_PRESENCE_SECONDS and DESKTOP_REQUEST_SECONDS in
 * app/core/application_updates.py), so two intervals must stay under 75 s:
 * one missed report must not make the updater vanish. Phase
 * changes are pushed at once. A cancellable local control subscription wakes
 * queued web actions immediately; older workers and the rollback flag retain
 * the same one-interval fallback. Hints never carry or consume commands.
 */
export const BRIDGE_INTERVAL_MS = 30_000;

export class Updater {
  private state: UpdateState = { phase: 'unsupported', version: null, percent: null, error: null, checkedAt: null };
  private listeners = new Set<(state: UpdateState) => void>();
  private timer: ReturnType<typeof setInterval> | null = null;
  private initialCheck: ReturnType<typeof setTimeout> | null = null;
  private applications: SourceUpdates | null = null;
  /** macOS and legacy Linux: signed installers rather than in-place updates. */
  private installers: Pick<InstallerUpdates, 'check' | 'download'> | null = null;
  private saved: (file: string) => void = () => {};
  private installedExecutable: string | undefined;
  private restartArgs: string[] = [];
  private checking = false;
  private downloading = false;
  private installing = false;
  private beforeInstall: () => Promise<void> = async () => {};
  private installFailed: (error: unknown) => Promise<void> = async () => {};
  private installGuard: () => Promise<string | null> = async () => null;
  private restarting: () => void = () => {};
  private restartCancelled: () => void = () => {};
  private prepared: { prepare: () => Promise<void>; discard: () => Promise<void> } | null = null;
  private failedStarts: FailedStartRecovery | null = null;
  private policy: () => Promise<boolean> = async () => true;
  private bridge: ((state: UpdateState) => Promise<Record<string, unknown>>) | null = null;
  private safeToRestart: () => Promise<boolean> = async () => false;
  private bridgeTimer: ReturnType<typeof setInterval> | null = null;
  private watchBridge: ((wake: () => void) => () => void) | null = null;
  private stopBridgeWatch: (() => void) | null = null;
  private exchanging = false;
  private exchangeAgain = false;
  private installed: InstalledRelease | null = null;
  private channelError: { message: string; attempt: number } | null = null;
  setBridge(exchange: (state: UpdateState) => Promise<Record<string, unknown>>, safeToRestart: () => Promise<boolean>, watch?: (wake: () => void) => () => void): void {
    this.bridge = exchange; this.safeToRestart = safeToRestart; this.watchBridge = watch ?? null;
  }
  async exchange(): Promise<void> {
    if (!this.bridge) return;
    if (this.exchanging) { this.exchangeAgain = true; return; }
    this.exchanging = true;
    try {
      const state = this.current;
      const channel_config = await this.applications?.channelConfig();
      const installed_release = await this.installedRelease();
      const refusal = this.channelError;
      const request = await this.bridge({ ...state, ...(channel_config ? { channel_config } : {}),
        ...(installed_release ? { installed_release } : {}),
        ...(refusal ? { channel_error: refusal.message, channel_attempt: refusal.attempt } : {}) });
      // Delivered: the worker has dropped the refused request.
      if (refusal && this.channelError === refusal) this.channelError = null;
      if (!this.bridgeTimer || typeof request.expires_at !== 'number' || request.expires_at * 1000 < Date.now()) return;
      if (request.action === 'set_channel' && (request.channel === 'stable' || request.channel === 'nightly')) {
        if (this.checking || this.downloading || this.installing || !this.applications) return;
        if (request.channel === 'stable' && typeof request.stable_manifest === 'string' && await this.applications.channel() === 'nightly') {
          // The launcher's setChannel refuses every return to stable. The
          // worker checked this one is safe; channel-return verifies it again.
          try { await returnToStable(this.applications, request.stable_manifest, app.getVersion()); }
          catch (error) {
            // The worker accepts at most 1000 characters; a longer report would fail every heartbeat.
            const message = (error instanceof Error ? error.message : String(error)).slice(0, 1000);
            this.channelError = { message, attempt: typeof request.attempt === 'number' ? request.attempt : 0 };
            this.exchangeAgain = true;
            return;
          }
        } else await this.applications.setChannel(request.channel);
        this.set({ phase: 'idle', version: null, release: null, notes: null, error: null });
        this.exchangeAgain = true;
      }
      else if (request.action === 'check') void this.check();
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
  /** Read once per running revision; null without a source updater. */
  private async installedRelease(): Promise<InstalledRelease | null> {
    const applications = this.applications;
    if (!applications?.version) return null;
    if (this.installed?.revision !== applications.version) {
      try { this.installed = await installedRelease(applications, app.getVersion()); }
      catch { return null; }
    }
    return this.installed;
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
  /**
   * Checked before an install stops the worker: a reason leaves everything
   * running and is reported as the update error, which the web UI shows. The
   * bridge refuses an install while sessions run; install() is also reachable
   * directly (desktop:update-install), so the same rule applies here. Nothing
   * asks natively: the in-app update button is the one place to retry.
   */
  onInstallGuard(guard: () => Promise<string | null>): void { this.installGuard = guard; }
  /**
   * Runs just before the app quits to start the update. The window closes and
   * the new version's can take minutes to appear on a first start, so the
   * owner is told here that it is starting (a system notification).
   */
  onRestarting(action: () => void): void { this.restarting = action; }
  /** Close restart notices even when a retained point forbids restarting the stack. */
  onRestartCancelled(action: () => void): void { this.restartCancelled = action; }
  /**
   * Runs once the update is pending and the stack is stopped, before the app
   * quits: the app makes the update's database restore point here, with its
   * window open, so the launcher only checks it (update-restore-point.ts).
   * `discard` runs when the install then fails, before the update stops being
   * pending: the running version will use the database again, so a point made
   * from it must never be kept for a later start.
   */
  onInstallPrepared(prepare: () => Promise<void>, discard: () => Promise<void>): void { this.prepared = { prepare, discard }; }
  /** A check refused because an update's first start failed: retry it when the launcher put everything back. */
  onFailedStart(recovery: FailedStartRecovery): void { this.failedStarts = recovery; }
  /** The check again after releasing failed starts, or null when none could be released. */
  private async retryFailedStart(error: unknown, interactive: boolean): Promise<{ result: Awaited<ReturnType<SourceUpdates['check']>>; released: string[] } | null> {
    if (!this.failedStarts || !this.applications || !FAILED_START.test(String(error))) return null;
    let released: string[];
    try { released = await this.failedStarts.release(); }
    catch (failure) { console.error('Could not offer the failed update again', failure); return null; }
    if (!released.length) return null;
    return { result: await this.applications.check(undefined, interactive), released };
  }
  useApplications(applications: SourceUpdates, installedExecutable?: string, restartArgs: string[] = []): void {
    if (!this.releaseUpdates || process.env.SHUFFLE_DESKTOP_NO_UPDATES === '1') return;
    this.applications = applications;
    if (process.platform === 'linux' && installedExecutable) applications.useInstalledElectron?.(process.versions.electron);
    applications.stageDownloads?.();
    this.installedExecutable = installedExecutable;
    this.restartArgs = restartArgs;
    const pending = applications.pendingVersion !== applications.version ? applications.pendingVersion : null;
    const cleanupError = applications.cleanupError ?? null;
    // Modern managers must validate cleanup before advertising another restart.
    // Legacy managers retain their existing synchronous pending-update behavior.
    const needsCheck = !!pending && !!applications.retryCleanup;
    this.set({ phase: cleanupError ? 'error' : pending && !needsCheck ? 'ready' : 'idle', version: pending?.slice(0, 12) ?? null, error: cleanupError, message: null, percent: null, release: null, notes: null });
    if (needsCheck && !cleanupError) void this.check();
    // Older source managers have no releaseInfo; the prompt then shows no notes.
    // The modern check returns the staged notes and any compatibility notice.
    // A parallel metadata read must not overwrite that notice after the check.
    if (pending && !needsCheck) void applications.releaseInfo?.(pending).then(info => {
      if (this.state.phase === 'ready' && this.state.version === pending.slice(0, 12)) this.set({ release: info.release ?? null, notes: info.notes ?? null });
    }).catch(() => {});
  }
  /**
   * macOS and legacy Linux: check the signed installer list on launch and daily (D22,
   * no consent prompt) and offer newer installers. Download saves and verifies
   * the installer and hands its path to saved(); the owner runs it. Windows and
   * stamped Linux use useApplications instead; a source-managed app never uses both.
   */
  useInstallers(installers: Pick<InstallerUpdates, 'check' | 'download'>, saved: (file: string) => void): void {
    if (!this.releaseUpdates || process.env.SHUFFLE_DESKTOP_NO_UPDATES === '1' || this.applications) return;
    this.installers = installers; this.saved = saved;
    this.set({ phase: 'idle', version: null, error: null, message: null, percent: null, release: null, notes: null });
  }
  get current(): UpdateState {
    const staged = this.applications?.pendingVersion;
    return { ...this.state, running_version: this.applications?.version ?? null,
      staged_version: staged && staged !== this.applications?.version ? staged : null };
  }
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
      if (!['0', 'false', 'off'].includes((process.env.AI_SHUFFLE_PERF_DESKTOP_UPDATE_PUSH ?? '').trim().toLowerCase())) {
        this.stopBridgeWatch = this.watchBridge?.(() => { if (this.bridgeTimer) void this.exchange(); }) ?? null;
      }
      void this.exchange();
    }
    if ((!this.applications && !this.installers) || this.timer) return;
    this.initialCheck = setTimeout(() => { this.initialCheck = null; void this.check(); }, 15_000);
    // Once on launch (above), then daily; users can still check from the app.
    this.timer = setInterval(() => void this.check(), 24 * 60 * 60 * 1000);
  }
  stop(): void { this.stopBridgeWatch?.(); this.stopBridgeWatch = null; if (this.bridgeTimer) clearInterval(this.bridgeTimer); this.bridgeTimer = null; if (this.initialCheck) clearTimeout(this.initialCheck); this.initialCheck = null; if (this.timer) clearInterval(this.timer); this.timer = null; }
  async check(interactive = false): Promise<void> {
    if ((!this.applications && !this.installers) || this.checking || this.downloading || this.installing || (this.state.phase === 'ready' && !this.applications?.retryCleanup)) return;
    this.checking = true; this.set({ phase: 'checking', error: null, percent: null, message: 'Checking for updates…', installer: null });
    try {
      if (!await this.permitted()) { this.set({ phase: 'idle' }); return; }
      if (!this.applications) {
        const found = await this.installers!.check();
        this.set(found.kind === 'available'
          ? { phase: 'available', version: found.version, percent: null, message: `AI Shuffle ${found.version} is available. Download the installer to update.`, checkedAt: new Date().toISOString(), release: found.release, notes: null }
          : { phase: 'idle', version: null, percent: null, message: null, checkedAt: new Date().toISOString(), release: null, notes: null });
        return;
      }
      let result: Awaited<ReturnType<SourceUpdates['check']>>;
      let released: string[] = [];
      try { result = await this.applications.check(undefined, interactive); }
      catch (error) {
        const retry = await this.retryFailedStart(error, interactive);
        if (!retry) throw error;
        ({ result, released } = retry);
      }
      const retried = !!result.version && released.some(revision => revision.startsWith(result.version!));
      this.set({ phase: result.kind === 'current' ? 'idle' : result.kind, version: result.version ?? null, percent: null, message: retried ? RETRY_MESSAGE : null, retry_start: retried, checkedAt: new Date().toISOString(), release: result.release ?? null, notes: [result.notice, result.notes].filter(Boolean).join('\n\n') || null, installer_newer: null });
    } catch (error) {
      // Nothing to retry: point at the installer only when it is a newer release than this one.
      const installerNewer = this.failedStarts && FAILED_START.test(String(error)) ? await this.failedStarts.installerNewer().catch(() => null) : null;
      this.set({ phase: 'error', error: String(error), message: null, installer_newer: installerNewer });
    }
    finally { this.checking = false; }
  }
  async download(): Promise<void> {
    if ((!this.applications && !this.installers) || this.checking || this.downloading || this.installing || !['available', 'error'].includes(this.state.phase)) return;
    this.downloading = true; this.set({ phase: 'downloading', error: null, percent: null, message: 'Preparing update…', installer: null });
    try {
      if (!await this.permitted()) { this.set({ phase: 'available' }); return; }
      if (!this.applications) {
        const { file, version } = await this.installers!.download((percent, message) => this.set({ percent, message }));
        this.set({ phase: 'idle', version: null, percent: null, error: null, release: null, notes: null, message: `AI Shuffle ${version} was downloaded and verified. Open ${file} to install it.`, installer: file });
        this.saved(file);
        return;
      }
      const result = await this.applications.download((percent, message) => this.set({ percent, message }), true);
      this.set({ phase: result.kind === 'current' ? 'idle' : result.kind, version: result.version ?? null, percent: null, message: null, release: result.release ?? null, notes: [result.notice, result.notes].filter(Boolean).join('\n\n') || null, retry_start: false });
    } catch (error) {
      // A newer release failed to download over a staged one: that one stays ready to install (#98).
      const staged = this.applications?.pendingVersion;
      if (staged && staged !== this.applications!.version && !this.applications!.cleanupError) {
        this.set({ phase: 'ready', version: staged.slice(0, 12), error: String(error), percent: null, message: null, release: null, notes: null });
        void this.applications!.releaseInfo?.(staged).then(info => {
          if (this.state.phase === 'ready' && this.state.version === staged.slice(0, 12)) this.set({ release: info.release ?? null, notes: info.notes ?? null });
        }).catch(() => {});
      } else this.set({ phase: 'error', error: String(error), percent: null, message: null });
    }
    finally { this.downloading = false; }
  }
  /** Never lets a notification failure stop the restart. */
  private announceRestart(): void {
    this.set({ message: RESTARTING_MESSAGE });
    try { this.restarting(); } catch (error) { console.error('Could not announce the restart', error); }
  }
  async install(): Promise<void> {
    if (!this.applications || this.state.phase !== 'ready' || this.checking || this.downloading || this.installing) return;
    this.installing = true; this.set({ error: null, message: null });
    let stopping = false;
    try {
      if (!await this.permitted()) { this.installing = false; return; }
      const refused = await this.installGuard();
      if (refused) { this.set({ error: refused }); this.installing = false; return; }
      // A lock can appear after a ready check. Refuse before stopping services.
      await this.applications.retryCleanup?.();
      stopping = true;
      await timedPhase('update install: stop worker and database', () => this.beforeInstall());
      await timedPhase('update install: prepare', async () => { await this.applications!.prepareInstall?.(); });
      if (this.prepared) await timedPhase('update install: database restore point', () => this.prepared!.prepare());
      if (this.installedExecutable) {
        // Electron's relaunch inherits this process's environment. An older
        // installed launcher treats these variables as an instruction to attach
        // to the running revision, bypassing selection of the pending update.
        const launchKeys = ['SHUFFLE_SOURCE_ROOT', 'SHUFFLE_SOURCE_REVISION', 'SHUFFLE_SOURCE_NODE'] as const;
        const saved = launchKeys.map(key => process.env[key]);
        for (const key of launchKeys) delete process.env[key];
        try {
          this.announceRestart();
          console.log(`[${new Date().toISOString()}] update install: restarting through the installed launcher`);
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
      this.announceRestart();
      app.relaunch({ execPath: current.node, args: [path.join(current.desktopDir, 'scripts/bootstrap.mjs'), '--root', this.applications.options.root] });
      app.quit();
    } catch (error) {
      try { this.restartCancelled(); } catch (failure) { console.error('Could not close the restart notice', failure); }
      if (stopping && this.prepared) {
        try { await this.prepared.discard(); }
        catch (failure) {
          // The copy for the update is still ready, so the update stays pending and
          // the launcher starts it next. Reopening the database here would let the
          // launcher later restore that copy over whatever this version wrote.
          console.error('Could not discard the update restore point', failure);
          this.set({ phase: 'ready', error: `${String(error)}\n${KEPT_POINT_MESSAGE}`, message: null });
          this.installing = false;
          return;
        }
      }
      try { await this.applications.cancelInstall?.(); } catch (failure) { error = failure; }
      // From what is left: a failed copy already ended the install, so nothing is staged.
      this.set({ phase: this.applications.cleanupError || this.applications.pendingVersion === null ? 'error' : 'ready', error: String(error), message: null });
      if (stopping) {
        try { await this.installFailed(error); }
        catch (failure) { this.set({ error: `${String(error)}\nRestart failed: ${String(failure)}` }); }
      }
      this.installing = false;
    }
  }
}
