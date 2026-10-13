/**
 * Join another worker's network with its invite: paste the invite link (or
 * the QR payload), or type the address and code. The inviter admits this
 * worker on contact; `status` (polled by the caller) drives the pending,
 * syncing and failed states. With `replace`, an enrolled worker leaves its
 * own network for the invited one after a confirm step.
 *
 * Shared by the setup wizard's JoinNetworkPanel and Devices > This worker >
 * Network.
 */
import { getErrorMessage } from '$lib/api/client';
import { cancelEnrollment, startEnrollment, type NetworkStatus } from '$lib/api/network';
import { looksLikePairingLink, parseJoinLink } from '$lib/api/network-status';

export interface JoinFlowOptions {
	status: () => NetworkStatus | null | undefined;
	replace?: () => boolean;
	deviceName?: () => string;
	onStarted?: () => void;
	onChanged?: () => void;
}

/** A bare hand-typed code (no link) of the invite's length. */
const BARE_CODE = /^[A-Za-z0-9-\s]{12,15}$/;

export class JoinFlow {
	input = $state('');
	endpoint = $state('');
	code = $state('');
	busy = $state(false);
	error = $state('');
	/** `replace`: the first Join asks; the second leaves and joins. */
	confirmReplace = $state(false);

	#opts: JoinFlowOptions;

	constructor(opts: JoinFlowOptions) {
		this.#opts = opts;
	}

	get replace(): boolean {
		return this.#opts.replace?.() ?? false;
	}

	get parsed() {
		return parseJoinLink(this.input);
	}

	get pairingLinkPasted(): boolean {
		return looksLikePairingLink(this.input);
	}

	get enrollment() {
		const status = this.#opts.status();
		return status && !status.enrolled ? (status.enrollment ?? null) : null;
	}

	get pending(): boolean {
		const s = this.enrollment?.status;
		return s === 'pending' || s === 'syncing';
	}

	get failed(): boolean {
		const e = this.enrollment;
		return e !== null && !this.pending && e.status !== 'complete';
	}

	get ready(): boolean {
		const parsed = this.parsed;
		return parsed ? Boolean(parsed.endpoint && parsed.code) : Boolean(this.endpoint.trim() && this.code.trim());
	}

	/** The invite field changed: a bare code also fills the manual Code field. */
	inputChanged(): void {
		const text = this.input.trim();
		if (!this.parsed && BARE_CODE.test(text) && !this.input.includes('://')) this.code = text;
	}

	/**
	 * Join. With `replace` the first call only arms the confirm unless
	 * `confirmed` says the caller already asked.
	 */
	async join(confirmed = false): Promise<void> {
		if (!this.ready || this.busy || this.pairingLinkPasted) return;
		const replace = this.replace;
		if (replace && !this.confirmReplace && !confirmed) {
			this.confirmReplace = true;
			return;
		}
		this.busy = true;
		this.error = '';
		try {
			const name = this.#opts.deviceName?.().trim() || undefined;
			const extra = { ...(name ? { device_name: name } : {}), ...(replace ? { replace: true } : {}) };
			await startEnrollment(
				this.parsed ? { link: this.input, ...extra } : { endpoint_url: this.endpoint, code: this.code, ...extra }
			);
			this.input = '';
			this.endpoint = '';
			this.code = '';
			this.confirmReplace = false;
			this.#opts.onStarted?.();
			this.#opts.onChanged?.();
		} catch (e) {
			this.error = getErrorMessage(e, 'Could not join');
		} finally {
			this.busy = false;
		}
	}

	keepCurrent(): void {
		this.confirmReplace = false;
	}

	/** Cancel a pending join, or dismiss a failed one. */
	async cancel(): Promise<void> {
		this.busy = true;
		this.error = '';
		try {
			await cancelEnrollment();
			this.#opts.onChanged?.();
		} catch (e) {
			this.error = getErrorMessage(e, 'Could not cancel');
		} finally {
			this.busy = false;
		}
	}
}
