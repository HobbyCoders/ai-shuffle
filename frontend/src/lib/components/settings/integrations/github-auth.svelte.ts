/**
 * GitHub sign-in for the worker's `gh` CLI: status, web device sign-in with
 * polling, personal access token and sign-out. Same rules as the setup
 * wizard's GitHubConnection: "connected" is only shown after the status
 * endpoint reads it back, the web code expires after 15 minutes, and a
 * newer attempt invalidates an older poll.
 */
import { copyText, COPY_ERROR_MESSAGE } from '$lib/utils/clipboard';
import {
	cancelGithubWebLogin,
	getGithubAuthStatus,
	loginGithubWithToken,
	logoutGithub,
	startGithubWebLogin
} from '$lib/api/auth';
import { getErrorMessage } from '$lib/api/client';
import { isForbidden } from '$lib/settings/access.svelte';
import { auth } from '$lib/stores/auth.svelte';

export const GITHUB_WEB_LOGIN_MS = 15 * 60 * 1000;
export const GITHUB_POLL_MS = 3000;
export const GITHUB_DEVICE_URL = 'https://github.com/login/device';
export const GITHUB_TOKENS_URL = 'https://github.com/settings/tokens';

export type GitHubMethod = 'web' | 'token';

export class GitHubAuthFlow {
	/** null until the first status read answers. */
	authenticated = $state<boolean | null>(null);
	user = $state<string | null>(null);
	configDir = $state<string | null>(null);
	/** The status read was refused (unapproved device). */
	forbidden = $state(false);
	method = $state<GitHubMethod>('web');
	token = $state('');
	code = $state('');
	busy = $state(true);
	error = $state('');
	copied = $state(false);

	#timer: ReturnType<typeof setTimeout> | undefined;
	#deadline = 0;
	#disposed = false;
	#generation = 0;

	/** Read the status once; call on mount. */
	async load(): Promise<void> {
		this.busy = true;
		try {
			await this.#readStatus();
			this.forbidden = false;
		} catch (e) {
			if (isForbidden(e)) this.forbidden = true;
			else this.error = getErrorMessage(e, 'Could not load GitHub status.');
		} finally {
			this.busy = false;
		}
	}

	dispose(): void {
		this.#disposed = true;
		this.#stopPolling();
	}

	setMethod(method: GitHubMethod): void {
		if (this.code) return;
		this.method = method;
		this.error = '';
	}

	async #readStatus() {
		const status = await getGithubAuthStatus();
		if (!this.#disposed) {
			this.authenticated = status.authenticated;
			this.user = status.user;
			this.configDir = status.config_dir || null;
		}
		return status;
	}

	#stopPolling() {
		this.#generation += 1;
		clearTimeout(this.#timer);
		this.code = '';
	}

	async #poll(id: number): Promise<void> {
		if (this.#disposed || id !== this.#generation) return;
		if (Date.now() >= this.#deadline) {
			this.#stopPolling();
			this.error = 'GitHub web sign-in expired. Please try again.';
			await cancelGithubWebLogin().catch(() => {});
			return;
		}
		try {
			const status = await getGithubAuthStatus();
			if (this.#disposed || id !== this.#generation) return;
			if (status.authenticated) {
				this.authenticated = true;
				this.user = status.user;
				this.configDir = status.config_dir || null;
				this.error = status.web_login_error || '';
				this.#stopPolling();
				await auth.checkAuth();
				return;
			}
			if (status.web_login_error) {
				this.error = status.web_login_error;
				this.#stopPolling();
				return;
			}
			this.error = '';
		} catch (e) {
			if (this.#disposed || id !== this.#generation) return;
			this.error = getErrorMessage(e, 'Could not check GitHub sign-in. Retrying…');
		}
		if (!this.#disposed && id === this.#generation) this.#timer = setTimeout(() => this.#poll(id), GITHUB_POLL_MS);
	}

	async startWeb(): Promise<void> {
		this.busy = true;
		this.error = '';
		this.copied = false;
		this.#stopPolling();
		try {
			const result = await startGithubWebLogin();
			if (this.#disposed) return;
			if (!result.success || !result.user_code) {
				this.error = result.error || 'Could not start GitHub web sign-in.';
				return;
			}
			this.code = result.user_code;
			this.#deadline = Date.now() + GITHUB_WEB_LOGIN_MS;
			const id = this.#generation;
			this.#timer = setTimeout(() => this.#poll(id), GITHUB_POLL_MS);
		} catch (e) {
			this.error = getErrorMessage(e, 'Could not start GitHub web sign-in.');
		} finally {
			this.busy = false;
		}
	}

	async cancelWeb(): Promise<void> {
		this.busy = true;
		this.error = '';
		this.#stopPolling();
		try {
			await cancelGithubWebLogin();
		} catch (e) {
			this.error = getErrorMessage(e, 'Could not cancel GitHub sign-in.');
		} finally {
			this.busy = false;
		}
	}

	async connectToken(): Promise<void> {
		if (!this.token.trim()) return;
		this.busy = true;
		this.error = '';
		try {
			const result = await loginGithubWithToken(this.token.trim());
			if (!result.success) {
				this.error = result.error || result.message || 'GitHub rejected this token.';
				return;
			}
			const status = await this.#readStatus();
			if (!status.authenticated) {
				this.error = 'GitHub sign-in could not be confirmed. Please retry.';
				return;
			}
			this.token = '';
			await auth.checkAuth();
		} catch (e) {
			this.error = getErrorMessage(e, 'Could not connect to GitHub.');
		} finally {
			this.busy = false;
		}
	}

	async disconnect(): Promise<void> {
		this.busy = true;
		this.error = '';
		try {
			const result = await logoutGithub();
			if (!result.success) {
				this.error = result.error || 'Could not disconnect GitHub.';
				return;
			}
			const status = await this.#readStatus();
			if (status.authenticated)
				this.error = 'GitHub is still connected. Check the server’s GitHub environment settings.';
			await auth.checkAuth();
		} catch (e) {
			this.error = getErrorMessage(e, 'Could not disconnect GitHub.');
		} finally {
			this.busy = false;
		}
	}

	async copyCode(): Promise<void> {
		try {
			await copyText(this.code);
			this.copied = true;
		} catch {
			this.copied = false;
			this.error = COPY_ERROR_MESSAGE;
		}
	}
}
