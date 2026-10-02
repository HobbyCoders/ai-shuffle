/**
 * The Shuffle account this worker belongs to: link state polling, browser or
 * phone sign-in, the profile (username, account workers and devices), the
 * username form, sign-out and the tunnel-limit refusal.
 *
 * Shared by the setup wizard's AccountPanel and Settings > Devices > Shuffle
 * account. Rules kept from AccountPanel:
 * - The browser tab is reserved inside the click (popup blockers), its
 *   opener cleared, and closed again when the offer fails.
 * - A pending offer is reused rather than minting a second worker code.
 * - The profile loads only once the link is active (`profile_ready`), again
 *   after a reconnect, and at most every 30s otherwise; older answers are
 *   dropped by version.
 * - A local sign-out can succeed with a remote-cleanup warning; it stays.
 */
import { copyText, COPY_ERROR_MESSAGE } from '$lib/utils/clipboard';
import { getErrorMessage } from '$lib/api/client';
import { auth } from '$lib/stores/auth.svelte';
import {
	beginAccountConnection,
	disconnectAccount,
	getAccountConnection,
	getAccountProfile,
	normalizeUsername,
	setAccountUsername,
	USERNAME_HINT,
	USERNAME_PATTERN,
	type AccountConnection,
	type AccountProfile,
	type TunnelLimitRefusal
} from '$lib/api/account';

export const ACCOUNT_POLL_MS = 4000;
export const ACCOUNT_INVENTORY_MS = 30_000;

export type AccountIntent = 'signin' | 'signup';

/** One-line remote access state, or '' when there is nothing to say. */
export function remoteAccessLabel(connection: AccountConnection | null): string {
	switch (connection?.status) {
		case 'connected':
			return 'Remote access is on';
		case 'connecting':
			return 'Turning on remote access…';
		case 'reconnecting':
			return 'Remote access is reconnecting';
		case 'limited':
			return 'Remote access is off: connection limit reached';
		case 'locked':
		case 'unavailable':
			return connection?.error ? '' : 'Remote access is unavailable on this worker';
		default:
			return '';
	}
}

/** "TESTONLYCODE" -> "TEST ONLY CODE" for reading aloud. */
export function groupCode(code: string): string {
	return code.match(/.{1,4}/g)?.join(' ') ?? code;
}

export class AccountFlow {
	connection = $state<AccountConnection | null>(null);
	profile = $state<AccountProfile | null>(null);
	/** True once the account inventory is known (or there is no account). */
	inventoryReady = $state(false);
	checking = $state(true);
	busy = $state(false);
	loadingProfile = $state(false);
	statusError = $state('');
	actionError = $state('');
	profileError = $state('');
	/** Which button opened the browser flow; decides which link is primary. */
	intent = $state<AccountIntent | null>(null);
	showPhone = $state(false);
	copied = $state(false);
	editingName = $state(false);
	usernameInput = $state('');
	savingUsername = $state(false);

	#workerName: () => string;
	#stopped = true;
	#requestVersion = 0;
	#profileVersion = 0;
	#profileCheckedAt = 0;
	#poll: ReturnType<typeof setTimeout> | undefined;

	constructor(workerName: () => string = () => 'My worker') {
		this.#workerName = workerName;
	}

	get canStart(): boolean {
		const c = this.connection;
		return c !== null && c.status !== 'locked' && c.status !== 'unavailable';
	}

	get signedIn(): boolean {
		return Boolean(this.connection?.signed_in);
	}

	get needsUsername(): boolean {
		return this.signedIn && this.profile !== null && !this.profile.account.profile_complete;
	}

	/** The account service refused this worker a remote connection. */
	get refusal(): TunnelLimitRefusal | null {
		const c = this.connection;
		return c?.signed_in && c.refusal?.code === 'tunnel_limit' ? c.refusal : null;
	}

	get remoteAccess(): string {
		return remoteAccessLabel(this.connection);
	}

	/** This browser signed in through the account (it can sign itself out). */
	get browserViaAccount(): boolean {
		return auth.current.status?.auth_mode === 'account';
	}

	/** The open offer, while the owner finishes it in the browser or on the phone. */
	get offerOpen(): boolean {
		return this.canStart && this.intent !== null && Boolean(this.connection?.code) && !this.connection?.signed_in;
	}

	/** Shown error, the refusal message excluded (it has its own notice). */
	get errorText(): string {
		return this.actionError || (this.connection?.error && !this.refusal ? this.connection.error : '');
	}

	/** Begin polling. Returns the stop function (for onMount). */
	start(): () => void {
		this.#stopped = false;
		void this.refresh();
		return () => this.stop();
	}

	stop(): void {
		this.#stopped = true;
		this.#requestVersion++;
		this.#profileVersion++;
		clearTimeout(this.#poll);
	}

	#scheduleRefresh() {
		clearTimeout(this.#poll);
		if (!this.#stopped) this.#poll = setTimeout(() => void this.refresh(), ACCOUNT_POLL_MS);
	}

	async refresh(): Promise<void> {
		if (this.busy) {
			this.#scheduleRefresh();
			return;
		}
		const version = ++this.#requestVersion;
		try {
			const next = await getAccountConnection();
			if (this.#stopped || version !== this.#requestVersion) return;
			const prev = this.connection;
			const wasSignedIn = prev?.signed_in ?? false;
			const wasProfilePending = prev?.profile_ready === false;
			const reconnected = prev?.status !== 'connected' && next.status === 'connected';
			this.connection = next;
			this.statusError = '';
			if (next.signed_in) {
				if (!wasSignedIn) this.inventoryReady = false;
				this.intent = null;
				this.showPhone = false;
				if (
					!wasSignedIn ||
					wasProfilePending ||
					reconnected ||
					Date.now() - this.#profileCheckedAt >= ACCOUNT_INVENTORY_MS
				)
					void this.loadProfile();
			} else {
				this.inventoryReady = true;
				this.#profileVersion++;
				this.#profileCheckedAt = 0;
				this.profile = null;
				this.profileError = '';
				this.editingName = false;
			}
		} catch (e) {
			if (!this.#stopped && version === this.#requestVersion)
				this.statusError = getErrorMessage(e, 'Could not check account status');
		} finally {
			if (!this.#stopped && version === this.#requestVersion) {
				this.checking = false;
				this.#scheduleRefresh();
			}
		}
	}

	async loadProfile(force = false): Promise<void> {
		if ((!force && this.loadingProfile) || this.savingUsername || !this.connection?.signed_in) return;
		if (this.connection.profile_ready === false) return;
		this.loadingProfile = true;
		this.#profileCheckedAt = Date.now();
		const version = ++this.#profileVersion;
		try {
			const next = await getAccountProfile();
			if (this.#stopped || version !== this.#profileVersion) return;
			this.profile = next;
			this.profileError = '';
			this.inventoryReady = true;
			if (!next.account.profile_complete && !this.editingName) this.usernameInput = next.account.username;
		} catch (e) {
			if (!this.#stopped && version === this.#profileVersion) {
				this.profileError = getErrorMessage(e, 'Could not load the account profile');
				this.inventoryReady = false;
			}
		} finally {
			if (version === this.#profileVersion) this.loadingProfile = false;
		}
	}

	/** Open the account page in a new tab (reserved in the click). */
	async begin(kind: AccountIntent): Promise<void> {
		const c = this.connection;
		if (this.busy || !this.canStart || !c || c.signed_in) return;
		this.intent = kind;
		this.actionError = '';
		this.copied = false;
		this.showPhone = false;
		const tab = window.open('about:blank', '_blank');
		if (tab) tab.opener = null;
		this.busy = true;
		this.#requestVersion++;
		try {
			// Reuse a pending offer rather than registering another worker.
			if (!c.code) this.connection = await beginAccountConnection(this.#workerName());
			const next = this.connection!;
			const url = kind === 'signup' ? next.signup_url || next.authorization_url : next.authorization_url;
			if (tab && url) tab.location.replace(url);
			else tab?.close();
			this.statusError = '';
		} catch (e) {
			tab?.close();
			this.intent = null;
			this.actionError = getErrorMessage(e, 'Could not start the account sign-in');
		} finally {
			this.busy = false;
			this.#scheduleRefresh();
		}
	}

	togglePhone(): void {
		this.showPhone = !this.showPhone;
	}

	async copyCode(): Promise<void> {
		const code = this.connection?.code;
		if (!code) return;
		try {
			await copyText(code);
			this.copied = true;
		} catch {
			this.copied = false;
			this.actionError = COPY_ERROR_MESSAGE;
		}
	}

	/** Sign this worker out, or cancel an open offer. */
	async signOut(): Promise<void> {
		if (this.busy) return;
		this.busy = true;
		this.actionError = '';
		this.#requestVersion++;
		this.#profileVersion++;
		try {
			const result = await disconnectAccount();
			if (result) {
				this.connection = result;
				// A local sign-out can succeed while remote cleanup fails; keep the warning.
				this.actionError = result.error || '';
				this.statusError = '';
			}
			this.profile = null;
			this.profileError = '';
			this.#profileCheckedAt = 0;
			this.inventoryReady = true;
			this.intent = null;
			this.showPhone = false;
			this.editingName = false;
		} catch (e) {
			this.actionError = getErrorMessage(e, 'Could not sign out');
		} finally {
			this.busy = false;
			this.#scheduleRefresh();
		}
	}

	startEdit(): void {
		this.usernameInput = this.profile?.account.username ?? '';
		this.profileError = '';
		this.editingName = true;
	}

	cancelEdit(): void {
		this.editingName = false;
	}

	/** Returns true when the worker saved the username. */
	async saveUsername(): Promise<boolean> {
		const username = normalizeUsername(this.usernameInput);
		if (!USERNAME_PATTERN.test(username)) {
			this.profileError = USERNAME_HINT;
			return false;
		}
		this.savingUsername = true;
		this.profileError = '';
		this.#profileVersion++;
		try {
			this.profile = await setAccountUsername(username);
			this.editingName = false;
			return true;
		} catch (e) {
			this.profileError = getErrorMessage(e, 'Could not save the username');
			return false;
		} finally {
			this.savingUsername = false;
		}
	}

	async signOutBrowser(): Promise<void> {
		try {
			await auth.logout();
		} catch (e) {
			this.actionError = getErrorMessage(e, 'Could not sign out of this browser');
		}
	}
}
