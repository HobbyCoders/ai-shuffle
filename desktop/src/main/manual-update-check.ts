import type { MessageBoxOptions } from 'electron';
import type { Updater } from './updater';

type UpdateActions = Pick<Updater, 'current' | 'check' | 'download' | 'install' | 'exchange'>;
type ShowDialog = (options: MessageBoxOptions) => Promise<{ response: number }>;
type FleetAction = 'check' | 'download' | 'install';

/** One worker as the update snapshot reports it (app/core/update_workers.py). */
export interface FleetWorker {
  device_id: string; device_name: string; is_self?: boolean; role?: string; label?: string;
  version?: string | null; release?: string | null; error?: string | null;
}
export interface FleetPlan {
  action: FleetAction; ready: boolean; preparing: boolean; available: boolean;
  staged: number; included: number; held: string; waiting: boolean;
}
export interface FleetSnapshot { allow_updates: boolean; update_all_workers: boolean; plan?: FleetPlan; workers: FleetWorker[] }
export interface FleetReceipt {
  scope?: string; sent?: string; held?: string;
  results: { device_name: string; accepted: boolean | null; reason: string }[];
}
/**
 * The worker's own update dispatch, the one the web UI's button uses. `workers`
 * resolves null when this app does not run the worker (an attached worker):
 * only then does the menu fall back to the local updater.
 */
export interface UpdateFleet {
  workers(): Promise<FleetSnapshot | null>;
  act(action: FleetAction): Promise<FleetReceipt>;
}
export interface ManualUpdateTiming {
  /** Between snapshot reads while something is checking or downloading. */
  pollMs?: number;
  /** How long a check may take before the result is shown anyway. */
  checkTimeoutMs?: number;
  /** How long a download is watched before the restart offer is left to the sidebar button. */
  downloadTimeoutMs?: number;
  sleep?: (ms: number) => Promise<void>;
}

const wait = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms));

/**
 * Native menu checks must report a result even when the web UI is hidden or
 * stale. They go through the worker exactly like the web UI's Check for
 * updates: the same request (check, then download, then install, as the
 * worker's plan says), the same all-workers scope, the same rule that restart
 * waits for every included worker, and the same refusals.
 */
export class ManualUpdateCheck {
  active = false;
  private readonly pollMs: number;
  private readonly checkTimeoutMs: number;
  private readonly downloadTimeoutMs: number;
  private readonly sleep: (ms: number) => Promise<void>;

  constructor(private readonly updater: UpdateActions, private readonly show: ShowDialog,
    private readonly fleet: UpdateFleet | null = null, timing: ManualUpdateTiming = {}) {
    this.pollMs = timing.pollMs ?? 2000;
    this.checkTimeoutMs = timing.checkTimeoutMs ?? 90_000;
    this.downloadTimeoutMs = timing.downloadTimeoutMs ?? 30 * 60_000;
    this.sleep = timing.sleep ?? wait;
  }

  async run(): Promise<void> {
    if (this.active) return;
    this.active = true;
    try {
      const snapshot = await this.read();
      if (snapshot?.plan) await this.runFleet(snapshot);
      else await this.runLocal();
    } finally {
      this.active = false;
    }
  }

  private async read(): Promise<FleetSnapshot | null> {
    if (!this.fleet) return null;
    try {
      return await this.fleet.workers();
    } catch (error) {
      console.error('Could not read the worker update plan; using the local updater', error);
      return null;
    }
  }

  /** Dispatch through the worker, then let the local shell take its queued part now rather than on its next 30 s report. */
  private async act(action: FleetAction): Promise<FleetReceipt> {
    const receipt = await this.fleet!.act(action);
    await this.updater.exchange();
    return receipt;
  }

  /** Re-read until `done`, the time runs out, or the plan cannot be read. */
  private async watch(snapshot: FleetSnapshot, done: (plan: FleetPlan) => boolean, timeoutMs: number): Promise<FleetSnapshot> {
    const until = Date.now() + timeoutMs;
    let current = snapshot;
    while (current.plan && !done(current.plan) && Date.now() < until) {
      await this.sleep(this.pollMs);
      const next = await this.read();
      if (!next?.plan) break;
      current = next;
    }
    return current;
  }

  private async runFleet(first: FleetSnapshot): Promise<void> {
    let snapshot = first;
    if (!snapshot.allow_updates) {
      await this.notice('info', 'Application updates are turned off', 'Turn them on in Settings > Account to check for updates.');
      return;
    }
    if (snapshot.plan!.waiting) {
      await this.notice('info', 'Waiting on workers to finish the previous update', workerLines(snapshot));
      return;
    }
    if (snapshot.plan!.action === 'check' && !snapshot.plan!.preparing) {
      let receipt: FleetReceipt;
      try {
        receipt = await this.act('check');
      } catch (error) {
        await this.notice('warning', 'Could not complete the update check', message(error));
        return;
      }
      const refused = refusals(receipt);
      if (refused && receipt.results.every(row => !row.accepted)) {
        await this.notice('warning', 'Could not complete the update check', refused);
        return;
      }
      snapshot = (await this.read()) ?? snapshot;
    }
    snapshot = await this.watch(snapshot, plan => !plan.preparing, this.checkTimeoutMs);
    await this.offer(snapshot);
  }

  /** Show what the plan offers and carry out the owner's choice. */
  private async offer(snapshot: FleetSnapshot): Promise<void> {
    const plan = snapshot.plan!;
    const lines = workerLines(snapshot);
    const label = releaseLabel(snapshot);
    if (plan.preparing) {
      await this.notice('info', 'An update is being prepared', `${lines}\n\nYou will be asked to restart once every worker is ready.`);
      return;
    }
    if (plan.held) {
      await this.notice('warning', 'Update needs attention', `${plan.held}\n\n${lines}`);
      return;
    }
    if (plan.action === 'check') {
      const failed = snapshot.workers.find(row => row.role === 'unknown' && row.error)?.error;
      if (failed) await this.notice('warning', 'Could not complete the update check', `${failed}\n\n${lines}`);
      else await this.notice('info', 'AI Shuffle is up to date', `No newer update is available on your selected update channel.\n\n${lines}`);
      return;
    }
    const install = plan.action === 'install';
    const { response } = await this.show({
      type: 'info', title: 'Check for updates',
      message: install ? 'An update is ready to install' : `AI Shuffle ${label} is available`,
      detail: install
        ? `AI Shuffle ${label} has been prepared${snapshot.update_all_workers ? ' on every worker' : ''}. Restart to finish installing it.\n\n${lines}`
        : `Download and prepare this update? Nothing will restart until you choose to install it.\n\n${lines}`,
      buttons: [install ? 'Restart and update' : 'Download update', 'Later'], defaultId: 1, cancelId: 1,
    });
    if (response !== 0) return;
    // A worker may have moved on while the dialog was open: act only on the same plan.
    const current = await this.read();
    if (!current?.plan || current.plan.action !== plan.action || current.plan.waiting) {
      await this.notice('info', 'The update status changed', 'Nothing was started. Choose Check for Updates again to see the current status.');
      return;
    }
    let receipt: FleetReceipt;
    try {
      receipt = await this.act(plan.action);
    } catch (error) {
      await this.notice('warning', install ? 'Could not start the update' : 'Could not download the update', message(error));
      return;
    }
    const refused = refusals(receipt);
    if (install) {
      // The worker holds the install and downloads on any worker still behind instead.
      if (refused || receipt.held) {
        await this.notice('warning', 'Update needs attention', [receipt.held, refused].filter(Boolean).join('\n\n'));
      }
      return;
    }
    if (refused) await this.notice('warning', 'Update needs attention', refused);
    else {
      // Not awaited: the download is watched while this note is open.
      void this.show({ type: 'info', title: 'Check for updates', message: 'Preparing the update',
        detail: 'You will be asked to restart once every worker is ready. The sidebar update button shows progress.',
        buttons: ['OK'], defaultId: 0, cancelId: 0 }).catch(() => undefined);
    }
    // Offer restart once every included worker is staged; a download still
    // running when the watch ends is left to the sidebar button.
    const after = (await this.read()) ?? current;
    const settled = await this.watch(after, next => !next.preparing, this.downloadTimeoutMs);
    if (settled.plan && !settled.plan.preparing) await this.offer(settled);
  }

  private async notice(type: 'info' | 'warning', text: string, detail: string): Promise<void> {
    await this.show({ type, title: 'Check for updates', message: text, detail, buttons: ['OK'], defaultId: 0, cancelId: 0 });
  }

  /** The worker cannot be reached: drive this app's own updater, as before. */
  private async runLocal(): Promise<void> {
    await this.updater.check(true);
    const state = { ...this.updater.current };
    const label = state.release || state.version || 'the new version';
    let message: string;
    let detail = state.message || '';
    let action: 'download' | 'install' | null = null;
    if (state.error) {
      message = 'Could not complete the update check';
      detail = state.error;
    } else if (state.phase === 'available') {
      message = `AI Shuffle ${label} is available`;
      detail ||= 'Download and prepare this update? Nothing will restart until you choose to install it.';
      action = 'download';
    } else if (state.phase === 'ready') {
      message = 'An update is ready to install';
      detail = `AI Shuffle ${label} has been prepared. Restart to finish installing it.`;
      action = 'install';
    } else if (state.phase === 'checking') {
      message = 'An update check is already running';
    } else if (state.phase === 'downloading') {
      message = 'An update is being prepared';
    } else if (state.phase === 'unsupported') {
      message = 'Updates are not supported by this app configuration';
    } else if (detail || !state.checkedAt) {
      message = 'Update checking is unavailable';
      detail ||= 'Updates are not available in this app configuration.';
    } else {
      message = 'AI Shuffle is up to date';
      detail = 'No newer update is available on your selected update channel.';
    }
    const { response } = await this.show({
      type: state.error ? 'warning' : 'info', title: 'Check for updates', message, detail,
      buttons: action ? [action === 'download' ? 'Download update' : 'Restart and update', 'Later'] : ['OK'],
      defaultId: action ? 1 : 0, cancelId: action ? 1 : 0,
    });
    // A heartbeat may have changed the phase while the dialog was open.
    if (response === 0 && action && this.updater.current.phase === state.phase && this.updater.current.version === state.version) {
      await this.updater[action]();
    }
  }
}

function workerLines(snapshot: FleetSnapshot): string {
  return snapshot.workers.map(row => `${row.device_name}: ${row.label ?? ''}`).join('\n');
}

function releaseLabel(snapshot: FleetSnapshot): string {
  const offered = snapshot.workers.find(row => row.role && row.role !== 'excluded' && (row.release || row.version));
  return offered?.release || offered?.version || 'the new version';
}

function refusals(receipt: FleetReceipt): string {
  return receipt.results.filter(row => !row.accepted).map(row => `${row.device_name}: ${row.reason}`).join('\n');
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
