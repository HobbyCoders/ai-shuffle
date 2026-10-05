/** Mounted-view state for owner-requested CLI jobs. No work continues after disposal. */
import { getErrorMessage } from '$lib/api/client';
import { getClaudeCliInstallation, installClaudeCli, updateClaudeCli, removeClaudeCli,
	setClaudeCliKeepInstalled, type ClaudeCliInstallation, type ClaudeCliJob } from '$lib/api/claude-cli';
import { auth } from '$lib/stores/auth.svelte';
import { providers } from '$lib/stores/providers.svelte';

export class ClaudeCliInstallState {
	status = $state<ClaudeCliInstallation | null>(null);
	error = $state('');
	busy = $state(false);
	now = $state(Date.now());
	#disposed = false;
	#timer: ReturnType<typeof setTimeout> | undefined;
	#onFinished: () => void | Promise<void>;
	#finished = '';
	constructor(onFinished: () => void | Promise<void> = () => {}) { this.#onFinished = onFinished; }
	get running() { return this.status?.job?.state === 'running'; }
	get elapsed() {
		const start = Date.parse(this.status?.job?.started_at ?? '');
		const seconds = Number.isFinite(start) ? Math.max(0, Math.floor((this.now - start) / 1000)) : 0;
		return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
	}
	async #accept(next: ClaudeCliInstallation) {
		if (this.#disposed) return;
		this.status = next;
		this.now = Date.now();
		clearTimeout(this.#timer);
		if (next.job?.state === 'running') {
			this.#timer = setTimeout(() => { void this.load(); }, 2000);
		} else if (next.job && this.#finished !== next.job.started_at) {
			this.#finished = next.job.started_at;
			await Promise.allSettled([providers.loadClaude(true), auth.checkAuth()]);
			if (!this.#disposed) await this.#onFinished();
		}
	}
	async load() {
		try { await this.#accept(await getClaudeCliInstallation()); this.error = ''; }
		catch (e) {
			if (this.#disposed) return;
			this.error = getErrorMessage(e, 'Could not check Claude Code. Try again.');
			if (this.running) this.#timer = setTimeout(() => { void this.load(); }, 2000);
		}
	}
	async start(kind: ClaudeCliJob['kind']) {
		if (this.busy || this.running || this.#disposed) return;
		this.busy = true; this.error = '';
		try { await this.#accept(await ({ install: installClaudeCli, update: updateClaudeCli, remove: removeClaudeCli }[kind])()); }
		catch (e) { this.error = getErrorMessage(e, 'Could not start the job. Try again.'); }
		finally { this.busy = false; }
	}
	async keep(value: boolean): Promise<boolean> {
		this.busy = true; this.error = '';
		try { await this.#accept(await setClaudeCliKeepInstalled(value)); return true; }
		catch (e) { this.error = getErrorMessage(e, 'Could not save the setting. Try again.'); return false; }
		finally { this.busy = false; }
	}
	dispose() { this.#disposed = true; clearTimeout(this.#timer); }
}
