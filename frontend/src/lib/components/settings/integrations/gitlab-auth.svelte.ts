/**
 * GitLab sign-in for this worker: a personal access token for gitlab.com or a
 * self-managed server. "Connected" is only shown after the status endpoint
 * reads the saved account back.
 */
import { getGitlabAuthStatus, loginGitlabWithToken, logoutGitlab } from '$lib/api/auth';
import { getErrorMessage } from '$lib/api/client';
import { isForbidden } from '$lib/settings/access.svelte';
import { auth } from '$lib/stores/auth.svelte';

export const GITLAB_DEFAULT_HOST = 'https://gitlab.com';

/** Token page on *host*, prefilled with a name and the scopes the app needs. */
export function gitlabTokensUrl(host: string): string {
	let base = (host || '').trim().replace(/\/+$/, '') || GITLAB_DEFAULT_HOST;
	if (!/^https?:\/\//i.test(base)) base = `https://${base}`;
	try {
		const url = new URL(base);
		if (url.protocol !== 'https:' && url.protocol !== 'http:') return `${GITLAB_DEFAULT_HOST}/-/user_settings/personal_access_tokens`;
	} catch {
		return `${GITLAB_DEFAULT_HOST}/-/user_settings/personal_access_tokens`;
	}
	return `${base}/-/user_settings/personal_access_tokens?name=AI+Shuffle&scopes=api,write_repository`;
}

export class GitLabAuthFlow {
	/** null until the first status read answers. */
	authenticated = $state<boolean | null>(null);
	user = $state<string | null>(null);
	host = $state<string | null>(null);
	/** The saved token is not usable right now (revoked, server down). */
	warning = $state('');
	/** The status read was refused (unapproved device). */
	forbidden = $state(false);
	serverInput = $state(GITLAB_DEFAULT_HOST);
	token = $state('');
	busy = $state(true);
	error = $state('');

	#disposed = false;

	async load(): Promise<void> {
		this.busy = true;
		try {
			await this.#readStatus();
			this.forbidden = false;
		} catch (e) {
			if (isForbidden(e)) this.forbidden = true;
			else this.error = getErrorMessage(e, 'Could not load GitLab status.');
		} finally {
			this.busy = false;
		}
	}

	dispose(): void {
		this.#disposed = true;
	}

	async #readStatus() {
		const status = await getGitlabAuthStatus();
		if (!this.#disposed) {
			this.authenticated = status.authenticated;
			this.user = status.user;
			this.host = status.host;
			this.warning = status.error || '';
		}
		return status;
	}

	async connect(): Promise<void> {
		const token = this.token.trim();
		if (!token) return;
		this.busy = true;
		this.error = '';
		try {
			const result = await loginGitlabWithToken(token, this.serverInput.trim() || GITLAB_DEFAULT_HOST);
			if (!result.success) {
				this.error = result.error || 'GitLab rejected this token.';
				return;
			}
			const status = await this.#readStatus();
			if (!status.authenticated) {
				this.error = 'GitLab sign-in could not be confirmed. Please retry.';
				return;
			}
			this.token = '';
			await auth.checkAuth();
		} catch (e) {
			this.error = getErrorMessage(e, 'Could not connect to GitLab.');
		} finally {
			this.busy = false;
		}
	}

	async disconnect(): Promise<void> {
		this.busy = true;
		this.error = '';
		try {
			const result = await logoutGitlab();
			if (!result.success) {
				this.error = result.error || 'Could not disconnect GitLab.';
				return;
			}
			await this.#readStatus();
			await auth.checkAuth();
		} catch (e) {
			this.error = getErrorMessage(e, 'Could not disconnect GitLab.');
		} finally {
			this.busy = false;
		}
	}
}
