/**
 * Google Antigravity facts the provider screens share: the static metadata
 * (auth kinds, Google's terms warning, links, models) and this worker's
 * install status. One copy for every screen; loaded on first use.
 */
import { getErrorMessage } from '$lib/api/client';
import {
	getAntigravityMeta,
	getAntigravityStatus,
	installAntigravity,
	updateAntigravity,
	type AntigravityInstallStatus,
	type AntigravityMeta
} from '$lib/api/antigravity';

const POLL_MS = 2000;

class AntigravityState {
	meta = $state<AntigravityMeta | null>(null);
	status = $state<AntigravityInstallStatus | null>(null);
	error = $state<string | null>(null);
	loading = $state(false);
	/** An install / Check for update request on its way to the worker. */
	starting = $state(false);
	/** What "Try again" repeats after `error`: the failed action, else a status read. */
	retry: (() => Promise<void>) | null = null;
	private poll: ReturnType<typeof setTimeout> | null = null;
	/** Bumped by every action and every poll scheduled: a status read that
	 * started before the latest one is stale when it lands and is dropped, so
	 * it can neither undo a newer error nor start a second polling chain. */
	private epoch = 0;

	/** Install job running on the worker. */
	get installing(): boolean {
		return this.status?.install?.state === 'running';
	}

	/** Install job running on the worker, or one being asked for. */
	get busy(): boolean {
		return this.starting || this.installing;
	}

	/** `force` re-reads the install status (the metadata is static). Screens
	 * force it when they open: a job may have run from another tab, another
	 * device or the startup check while this one wasn't looking. */
	async load(force = false): Promise<void> {
		if (this.loading || (!force && this.meta && this.status)) return;
		this.loading = true;
		const seen = this.epoch;
		try {
			const [meta, status] = await Promise.all([this.meta ?? getAntigravityMeta(), getAntigravityStatus()]);
			this.meta = meta;
			if (seen !== this.epoch) return; // an action or poll since owns the status
			this.status = status;
			this.error = null;
			if (status.install?.state === 'running' && !this.poll) this.schedule();
		} catch (e) {
			if (seen !== this.epoch) return;
			this.error = getErrorMessage(e, 'Could not read the Antigravity status');
			this.retry = () => this.load(true);
		} finally {
			this.loading = false;
		}
	}

	/** Install, or `update` (Check for update): one job on the worker either way. */
	async install(kind: 'install' | 'update' = 'install'): Promise<void> {
		if (this.busy) return;
		this.starting = true;
		this.error = null;
		this.cancelPoll();
		const mine = ++this.epoch;
		try {
			const install = await (kind === 'update' ? updateAntigravity() : installAntigravity());
			if (mine !== this.epoch) return;
			if (this.status) this.status = { ...this.status, install };
			this.schedule();
		} catch (e) {
			if (mine !== this.epoch) return;
			this.epoch++; // a status read begun while the request was out must not clear this error
			this.error = getErrorMessage(e, kind === 'update'
				? 'Could not check for an Antigravity update' : 'Could not start the Antigravity install');
			this.retry = () => this.install(kind);
		} finally {
			this.starting = false;
		}
	}

	private cancelPoll(): void {
		if (this.poll) clearTimeout(this.poll);
		this.poll = null;
	}

	private schedule(): void {
		this.cancelPoll();
		const mine = ++this.epoch;
		this.poll = setTimeout(async () => {
			try {
				let status = await getAntigravityStatus();
				if (mine === this.epoch && status.install?.state !== 'running') {
					// That read may have timed the old binary's --version just
					// before the job switched copies: read once more so Version
					// matches the result message.
					status = await getAntigravityStatus();
				}
				if (mine !== this.epoch) return; // superseded: a newer chain or action owns it
				this.status = status;
				this.poll = null;
				if (status.install?.state === 'running') this.schedule();
			} catch (e) {
				if (mine !== this.epoch) return;
				this.poll = null;
				this.error = getErrorMessage(e, 'Could not read the Antigravity status');
				this.retry = () => this.load(true);
			}
		}, POLL_MS);
	}
}

export const antigravity = new AntigravityState();
