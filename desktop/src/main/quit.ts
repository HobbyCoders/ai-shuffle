export interface QuitActions {
	activeSessionCount(): Promise<number>;
	confirm(activeCount: number | null): Promise<boolean>;
	/** The graceful stop: the worker stops its sessions, then everything shuts down. */
	stop(): Promise<void>;
	/** Kill the worker without asking it to stop sessions, then shut the rest down. */
	forceStop(): Promise<void>;
	quit(): void;
	/**
	 * Tell the user the graceful stop failed. With `offerForce` (a repeated
	 * failure) the dialog also offers to quit anyway; resolve true for that.
	 */
	failed(error: unknown, offerForce: boolean): Promise<boolean | void>;
}

export interface QuitRequest {
	/**
	 * Quit even if the worker is wedged (the tray's Quit): the graceful stop
	 * still runs first, and only when it fails or outlasts the grace period
	 * is the worker killed.
	 */
	force?: boolean;
}

/**
 * How long a forced quit waits for the graceful stop. The worker's own
 * stop-sessions answer is bounded at 30 s and the process kill at 13 s, so
 * this only matters when something below those bounds hangs.
 */
export const FORCE_QUIT_GRACE_MS = 60_000;

/** Serialize close requests so a second click cannot bypass confirmation. */
export class QuitGuard {
	private pending = false;
	private approved = false;
	private forceRequested = false;
	private failures = 0;
	private armGrace: (() => void) | null = null;

	constructor(private readonly actions: QuitActions, private readonly graceMs = FORCE_QUIT_GRACE_MS) {}

	get canQuit(): boolean { return this.approved; }

	async request({ force = false }: QuitRequest = {}): Promise<void> {
		if (this.approved) return;
		if (this.pending) {
			// A tray Quit during a pending close upgrades it: if its graceful stop fails, kill the worker.
			if (force) {
				this.forceRequested = true;
				this.armGrace?.();
			}
			return;
		}
		this.pending = true;
		this.forceRequested = force;
		try {
			let active: number | null;
			try { active = await this.actions.activeSessionCount(); }
			catch { active = null; }
			if (active !== 0 && !(await this.actions.confirm(active))) return;
			try {
				await this.graceful();
				this.failures = 0;
			} catch (error) {
				this.failures++;
				const quitAnyway = this.forceRequested || (await this.actions.failed(error, this.failures > 1)) === true;
				// A tray Quit while the failure dialog was open counts too.
				if (!quitAnyway && !this.forceRequested) return;
				await this.actions.forceStop();
			}
			this.approved = true;
			this.actions.quit();
		} catch (error) {
			await this.actions.failed(error, false);
		} finally {
			this.pending = false;
			this.forceRequested = false;
		}
	}

	/** The graceful stop; bounded by the grace period once a force quit was asked for. */
	private graceful(): Promise<void> {
		const stopping = this.actions.stop();
		return new Promise<void>((resolve, reject) => {
			let timer: ReturnType<typeof setTimeout> | null = null;
			// Armed now for a tray Quit, or later if one arrives while this stop runs.
			this.armGrace = () => {
				if (timer) return;
				timer = setTimeout(() => reject(new Error('The worker did not stop in time.')), this.graceMs);
			};
			if (this.forceRequested) this.armGrace();
			void stopping.then(resolve, reject).finally(() => {
				this.armGrace = null;
				if (timer) clearTimeout(timer);
			});
		});
	}
}
