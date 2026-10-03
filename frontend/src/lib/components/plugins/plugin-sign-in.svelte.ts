/**
 * Plugin service sign-in, shared by the private access card and
 * Settings > Plugins > Connections.
 *
 * `signInView` is the one place that decides what the owner is offered: the
 * single "Sign in with <host>" button, the manual client setup, or neither.
 * `SignInFlow` runs the provider window: it opens it from the click, waits
 * for the callback page's `plugin-oauth` message and, because a provider can
 * cut the window off from this page (or the owner closes it), re-reads the
 * status whenever this window regains focus.
 */
import { getErrorMessage } from '$lib/api/client';
import {
	startSignIn,
	type ClientSource,
	type ServiceSignIn,
	type SignInMessage,
	type SignInState
} from '$lib/api/plugin-oauth';

/** What the owner can do next for one service. */
export type SignInStep = 'sign_in' | 'setup' | 'signed_in' | 'plugin_off' | 'invalid';

export interface SignInView {
	step: SignInStep;
	/** A previous sign-in stopped working; `reason` says why. */
	again: boolean;
	/** Manual client entry: needed (no client from any source), available (the owner's own client, to change), or none. */
	manual: 'needed' | 'available' | 'none';
}

/**
 * Decide the offer for one service.
 *
 * The worker's `client_source` decides it: `owner`, `plugin` (the client the
 * plugin ships) and `registered` (registered automatically at sign-in) all
 * mean a signed-out service gets just the sign-in button; `none`, or
 * `not_configured` (including a refused registration, reason
 * `registration_failed`), means the owner enters a client: the manual
 * setup. Workers that predate `client_source` only know the owner's client,
 * `client_configured`.
 */
export function signInView(service: Pick<ServiceSignIn, 'state' | 'client_configured' | 'client_source'>, enabled: boolean): SignInView {
	const source = clientSource(service);
	const hasClient = service.state !== 'not_configured' && source !== 'none';
	const manual = !hasClient ? 'needed' : source === 'owner' ? 'available' : 'none';
	if (service.state === 'invalid') return { step: 'invalid', again: false, manual: 'none' };
	if (service.state === 'signed_in') return { step: 'signed_in', again: false, manual };
	const again = service.state === 'needs_sign_in';
	if (!hasClient) return { step: 'setup', again, manual };
	if (!enabled) return { step: 'plugin_off', again, manual };
	return { step: 'sign_in', again, manual };
}

/** Where the client comes from, with the fallback for workers that predate the field. */
export function clientSource(service: Pick<ServiceSignIn, 'client_configured' | 'client_source'>): ClientSource {
	return service.client_source ?? (service.client_configured ? 'owner' : 'none');
}

function hostOf(url: string | undefined): string {
	try { return url ? new URL(url).hostname : ''; } catch { return ''; }
}

/**
 * Which client a sign-in uses, for display: the owner's own, the one the
 * plugin ships, or one registered automatically at the plugin's
 * `registration_url`. `code` is the client id or host to show in mono;
 * `null` when there is no client yet (the manual setup says so).
 */
export function clientLine(
	service: Pick<ServiceSignIn, 'state' | 'client_configured' | 'client_source' | 'client_id' | 'plugin_client_id' | 'registration_url'>
): { text: string; code: string } | null {
	if (service.state === 'invalid' || service.state === 'not_configured') return null;
	switch (clientSource(service)) {
		case 'owner': return { text: 'Uses your client', code: service.client_id };
		case 'plugin': return { text: 'Uses the plugin’s client', code: service.plugin_client_id ?? '' };
		case 'registered': return { text: 'Set up automatically with', code: hostOf(service.registration_url) };
		default: return null;
	}
}

/**
 * What removing the owner's client falls back to, for the confirmation:
 * the plugin's client, automatic setup, or nothing.
 */
export function ownerClientFallback(service: Pick<ServiceSignIn, 'plugin_client_id' | 'registration_url'>): string {
	if (service.plugin_client_id) return 'Without it, sign-in uses the plugin’s own client.';
	if (service.registration_url) return 'Without it, sign-in sets itself up automatically.';
	return 'Without it, signing in needs a new client.';
}

const REASONS: Record<string, [long: string, short: string]> = {
	scopes_widened: ['The plugin now asks for more access, so sign in again.', 'Asks for more access'],
	endpoint_changed: ['The service’s sign-in address changed, so sign in again.', 'Sign-in address changed'],
	client_changed: ['The sign-in client changed, so sign in again.', 'Sign-in client changed'],
	expired: ['Your sign-in expired. Sign in again.', 'Sign-in expired'],
	unreadable: ['The saved sign-in could not be read. Sign in again.', 'Saved sign-in unreadable'],
	registration_failed: ['Automatic setup didn’t work, so this service needs a manual setup.', 'Automatic setup failed']
};

/** Why a sign-in is needed again, as a sentence (`short`: at most five words). */
export function reasonText(reason: string, short = false): string {
	const known = REASONS[reason];
	if (known) return short ? known[1] : known[0];
	return short ? 'Sign in again' : 'Sign in to this service again.';
}

/** The state in at most five words, for a settings row hint. */
export function stateHint(view: SignInView, reason: string): string {
	if (view.again && view.step !== 'invalid') return reasonText(reason, true);
	switch (view.step) {
		case 'signed_in': return 'Signed in';
		case 'sign_in': return 'Not signed in';
		case 'setup': return reason === 'registration_failed' ? reasonText(reason, true) : 'Needs one-time setup';
		case 'plugin_off': return 'Turn the plugin on';
		default: return 'Sign-in declaration is invalid';
	}
}

/** Read-only or read and write, from the declared access. */
export function accessText(access: string | undefined): string {
	return access === 'write' ? 'Read and write' : 'Read-only';
}

/**
 * The provider must send the browser back to an https:// address or to this
 * computer; a plain http:// LAN address is refused by the worker.
 */
export function returnAddressUsable(uri: string): boolean {
	try {
		const url = new URL(uri);
		return url.protocol === 'https:' || (url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname));
	} catch {
		return false;
	}
}

/** `dns-reader@my-marketplace` reads "dns-reader". */
export function pluginName(id: string): string {
	const at = id.lastIndexOf('@');
	return at > 0 ? id.slice(0, at) : id;
}

export type FlowPhase = 'idle' | 'waiting' | 'done' | 'error';

/**
 * One sign-in window at a time for one plugin service. `check` re-reads the
 * status (the caller keeps it on screen) and returns the new state.
 */
export class SignInFlow {
	phase = $state<FlowPhase>('idle');
	error = $state('');
	/** The owner came back without finishing; the window may still be open. */
	unfinished = $state(false);

	#plugin: string;
	#service: string;
	#check: () => Promise<SignInState | null>;
	#popup: Window | null = null;
	#flowId = '';
	#expiry: ReturnType<typeof setTimeout> | undefined;
	#listening = false;

	constructor(plugin: string, service: string, check: () => Promise<SignInState | null>) {
		this.#plugin = plugin;
		this.#service = service;
		this.#check = check;
	}

	/** Open the provider. Call straight from the click so the window is allowed. */
	async begin(): Promise<void> {
		if (this.phase === 'waiting') return;
		this.#stop();
		this.error = '';
		this.unfinished = false;
		// Opened before the request so the browser treats it as the click's
		// window. Not `noopener`: the callback page posts back to its opener.
		const popup = window.open('about:blank', '_blank', 'popup,width=560,height=740');
		if (!popup) {
			this.phase = 'error';
			this.error = 'Allow pop-ups for this app, then try again.';
			return;
		}
		this.#popup = popup;
		this.phase = 'waiting';
		try {
			const started = await startSignIn(this.#plugin, this.#service);
			if (this.#popup !== popup) return; // cancelled meanwhile
			this.#flowId = started.flow_id;
			this.#listen();
			this.#expiry = setTimeout(() => this.#expire(), Math.max(1, started.expires_in) * 1000);
			popup.location.href = started.authorization_url;
		} catch (e) {
			if (this.#popup !== popup) return;
			this.#stop();
			this.phase = 'error';
			// The worker's refusals are fixed wording and never carry a secret.
			this.error = getErrorMessage(e, 'The sign-in could not start. Try again.');
			// A refused automatic registration turns the service into
			// `not_configured` (`registration_failed`): re-read so the manual
			// setup appears.
			void this.#recheck();
		}
	}

	/** Stop waiting and close the provider window. */
	cancel(): void {
		this.#stop();
		this.phase = 'idle';
		this.unfinished = false;
	}

	/** Remove listeners and timers; for unmount. */
	dispose(): void {
		this.#stop();
	}

	#listen() {
		if (this.#listening) return;
		window.addEventListener('message', this.#onMessage);
		window.addEventListener('focus', this.#onFocus);
		this.#listening = true;
	}

	#stop() {
		if (this.#listening) {
			window.removeEventListener('message', this.#onMessage);
			window.removeEventListener('focus', this.#onFocus);
			this.#listening = false;
		}
		clearTimeout(this.#expiry);
		this.#expiry = undefined;
		this.#flowId = '';
		try { this.#popup?.close(); } catch { /* cut off by the provider */ }
		this.#popup = null;
	}

	async #recheck(): Promise<void> {
		try { await this.#check(); } catch { /* the caller shows its own load error */ }
	}

	#onMessage = (event: MessageEvent) => {
		const data = event.data as Partial<SignInMessage> | null;
		if (event.origin !== window.location.origin || !data || data.type !== 'plugin-oauth' || !this.#flowId || data.flow_id !== this.#flowId) return;
		this.#stop();
		this.unfinished = false;
		if (data.error) {
			this.phase = 'error';
			this.error = data.error;
		} else {
			this.phase = 'done';
		}
		void this.#check();
	};

	#onFocus = async () => {
		if (!this.#flowId) return;
		const state = await this.#check().catch(() => null);
		if (!this.#flowId) return; // the message settled it meanwhile
		if (state === 'signed_in') {
			this.#stop();
			this.phase = 'done';
			this.unfinished = false;
		} else if (this.phase === 'waiting') {
			// Keep listening until the flow expires: the window may still finish.
			this.phase = 'idle';
			this.unfinished = true;
		}
	};

	#expire() {
		const waiting = this.phase === 'waiting' || this.unfinished;
		this.#stop();
		this.unfinished = false;
		if (waiting) {
			this.phase = 'error';
			this.error = 'The sign-in expired before it finished. Start it again.';
		}
	}
}
