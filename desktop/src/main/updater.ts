/**
 * Auto-update through electron-updater.
 *
 * Updates are read from the GitHub release that matches the server's `v*`
 * tags: the release workflow uploads the installers and their `latest*.yml`
 * metadata next to the server packages, and electron-updater downloads a new
 * shell quietly in the background. The page shows the "Update ready" state
 * through the bridge and asks the shell to restart when the person is ready;
 * otherwise the update installs on the next quit.
 */
import { app } from 'electron';
import { autoUpdater, type UpdateInfo } from 'electron-updater';

export type UpdatePhase = 'idle' | 'checking' | 'available' | 'downloading' | 'ready' | 'error' | 'unsupported';

export interface UpdateState {
	phase: UpdatePhase;
	version: string | null;
	percent: number | null;
	error: string | null;
	checkedAt: string | null;
}

const CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;

export class Updater {
	private state: UpdateState = { phase: 'idle', version: null, percent: null, error: null, checkedAt: null };
	private listeners = new Set<(state: UpdateState) => void>();
	private timer: NodeJS.Timeout | null = null;
	private readonly enabled: boolean;

	constructor() {
		// Development runs have no update feed; unsigned macOS builds cannot
		// replace themselves either, so treat that as unsupported rather than
		// as a repeating error.
		this.enabled = app.isPackaged && !process.env.SHUFFLE_DESKTOP_NO_UPDATES;
		if (!this.enabled) {
			this.state = { ...this.state, phase: 'unsupported' };
			return;
		}
		autoUpdater.autoDownload = true;
		autoUpdater.autoInstallOnAppQuit = true;
		autoUpdater.allowPrerelease = false;
		autoUpdater.logger = null;

		autoUpdater.on('checking-for-update', () => this.set({ phase: 'checking', error: null }));
		autoUpdater.on('update-available', (info: UpdateInfo) => this.set({ phase: 'available', version: info.version }));
		autoUpdater.on('update-not-available', () => this.set({ phase: 'idle', version: null, percent: null, checkedAt: new Date().toISOString() }));
		autoUpdater.on('download-progress', (progress) => this.set({ phase: 'downloading', percent: Math.round(progress.percent) }));
		autoUpdater.on('update-downloaded', (info: UpdateInfo) => this.set({ phase: 'ready', version: info.version, percent: 100 }));
		autoUpdater.on('error', (error) => {
			this.set({ phase: 'error', error: error?.message ?? String(error), checkedAt: new Date().toISOString() });
		});
	}

	get current(): UpdateState {
		return { ...this.state };
	}

	subscribe(listener: (state: UpdateState) => void): () => void {
		this.listeners.add(listener);
		return () => this.listeners.delete(listener);
	}

	start(): void {
		if (!this.enabled) return;
		setTimeout(() => void this.check(), 15_000);
		this.timer = setInterval(() => void this.check(), CHECK_INTERVAL_MS);
	}

	stop(): void {
		if (this.timer) clearInterval(this.timer);
		this.timer = null;
	}

	async check(): Promise<void> {
		if (!this.enabled) return;
		if (this.state.phase === 'downloading' || this.state.phase === 'ready') return;
		try {
			await autoUpdater.checkForUpdates();
		} catch (error) {
			this.set({ phase: 'error', error: error instanceof Error ? error.message : String(error) });
		}
	}

	install(): void {
		if (!this.enabled || this.state.phase !== 'ready') return;
		setImmediate(() => autoUpdater.quitAndInstall(false, true));
	}

	private set(patch: Partial<UpdateState>): void {
		this.state = { ...this.state, ...patch };
		for (const listener of this.listeners) listener(this.current);
	}
}
