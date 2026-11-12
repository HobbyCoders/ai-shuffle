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
	type AntigravityInstallStatus,
	type AntigravityMeta
} from '$lib/api/antigravity';

const POLL_MS = 2000;

class AntigravityState {
	meta = $state<AntigravityMeta | null>(null);
	status = $state<AntigravityInstallStatus | null>(null);
	error = $state<string | null>(null);
	loading = $state(false);
	private poll: ReturnType<typeof setTimeout> | null = null;

	/** Install job running on the worker. */
	get installing(): boolean {
		return this.status?.install?.state === 'running';
	}

	async load(force = false): Promise<void> {
		if (this.loading || (!force && this.meta && this.status)) return;
		this.loading = true;
		try {
			const [meta, status] = await Promise.all([this.meta && !force ? this.meta : getAntigravityMeta(), getAntigravityStatus()]);
			this.meta = meta;
			this.status = status;
			this.error = null;
			if (status.install?.state === 'running') this.schedule();
		} catch (e) {
			this.error = getErrorMessage(e, 'Could not read the Antigravity status');
		} finally {
			this.loading = false;
		}
	}

	async install(): Promise<void> {
		try {
			const install = await installAntigravity();
			if (this.status) this.status = { ...this.status, install };
			this.error = null;
			this.schedule();
		} catch (e) {
			this.error = getErrorMessage(e, 'Could not start the Antigravity install');
		}
	}

	private schedule(): void {
		if (this.poll) clearTimeout(this.poll);
		this.poll = setTimeout(async () => {
			this.poll = null;
			try {
				this.status = await getAntigravityStatus();
				if (this.status.install?.state === 'running') this.schedule();
			} catch (e) {
				this.error = getErrorMessage(e, 'Could not read the Antigravity status');
			}
		}, POLL_MS);
	}
}

export const antigravity = new AntigravityState();
