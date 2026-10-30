import { getErrorMessage } from '$lib/api/client';
import { actOnWorkers, getWorkerSettings, type WorkerAction, type WorkerMachine, type WorkerResult } from '$lib/api/worker-settings';
import { FORBIDDEN_TEXT, isForbidden } from '$lib/settings/access.svelte';

export const OUTCOME_TEXT: Record<string, string> = {
	done: 'Started on this worker', saved: 'Saved', offline: 'Offline · not queued',
	unsupported: 'Update this worker to use machine settings', refused: 'Worker refused the request',
	uncertain: 'No confirmation · check status before retrying', busy: 'An install or update is already running',
	'not-installed': 'Install Claude CLI first', 'not-owned': 'This CLI is managed outside AI Shuffle',
	platform: 'Installation is not supported on this platform', 'channel-refused': 'Channel change refused by this worker’s launcher',
	'unknown-instance': 'Worker identity is not known yet; reconnect it first'
};
export function signInText(signedIn: boolean | null | undefined): string {
	return signedIn === true ? 'Signed in' : signedIn === false ? 'Not signed in' : 'Status unavailable';
}

export class MachineSettingsModel {
	workers = $state<WorkerMachine[]>([]);
	results = $state<WorkerResult[]>([]);
	error = $state('');
	stale = $state(false);
	loading = $state(false);
	saving = $state(false);
	#load: Promise<void> | null = null;
	#stopped = false;

	load(): Promise<void> {
		if (this.#load) return this.#load;
		this.loading = true;
		this.#load = (async () => {
			try {
				const data = await getWorkerSettings();
				if (this.#stopped) return;
				this.workers = data.workers;
				this.stale = false;
				this.error = '';
			} catch (e) {
				if (this.#stopped) return;
				this.stale = true;
				this.error = isForbidden(e) ? FORBIDDEN_TEXT : getErrorMessage(e, 'Could not read worker settings');
			} finally {
				this.loading = false;
				this.#load = null;
			}
		})();
		return this.#load;
	}

	async act(target: string, action: WorkerAction): Promise<boolean> {
		if (this.saving || this.stale) return false;
		this.saving = true;
		this.error = '';
		this.results = [];
		try {
			this.results = (await actOnWorkers(target, action)).workers;
			// Never re-send an uncertain action. Refresh the producing worker's status.
			await this.load();
			return this.results.every((r) => r.outcome === 'accepted');
		} catch (e) {
			this.error = isForbidden(e) ? FORBIDDEN_TEXT : getErrorMessage(e, 'Could not change worker settings');
			return false;
		} finally {
			this.saving = false;
		}
	}

	start(): () => void {
		this.#stopped = false;
		void this.load();
		const refresh = () => { if (document.visibilityState === 'visible') void this.load(); };
		const timer = setInterval(refresh, 5000);
		document.addEventListener('visibilitychange', refresh);
		return () => {
			this.#stopped = true;
			clearInterval(timer);
			document.removeEventListener('visibilitychange', refresh);
		};
	}
}
