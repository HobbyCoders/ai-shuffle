/**
 * System > Updates > Update channel: stable or nightly for this worker
 * (GET/PUT /api/v1/updates/channel, owner only; see PR #2299).
 *
 * A container saves the choice at once and answers with the saved channel;
 * `restart_required` stays true until it restarts. A desktop worker answers
 * 202 with the snapshot it had before the write: the request is queued, and
 * the launcher writes the file on its next heartbeat (about 30 s). So a PUT
 * whose answer does not show the new channel yet is followed by polling GET
 * until it does.
 *
 * The wait has its own deadline (CHANNEL_WAIT_MS), independent of the reads:
 * each read is abandoned after CHANNEL_READ_MS, so a hanging request cannot
 * keep the picker busy. After the deadline the request is still queued on the
 * worker and can still apply, so the picker never pretends it was cancelled.
 * It says the switch is pending, keeps checking every CHANNEL_LATE_POLL_MS,
 * and offers "Check status" and "Send again".
 *
 * Going from nightly back to stable needs a reinstall; the server refuses it
 * with 409 and the picker says why.
 */
import { api, getErrorMessage } from '$lib/api/client';
import { isForbidden } from '$lib/settings/access.svelte';
import { flashSaved } from '$lib/settings/save-status.svelte';

export type Channel = 'stable' | 'nightly';

export interface ChannelStatus {
	channel: Channel;
	available: Channel[];
	source: 'env' | 'file' | 'default';
	editable: boolean;
	restart_required: boolean;
}

export const CHANNEL_POLL_MS = 3_000;
/** Two missed heartbeats and a margin: the launcher is not answering. */
export const CHANNEL_WAIT_MS = 75_000;
/** One status read longer than this is abandoned and the next one starts. */
export const CHANNEL_READ_MS = 10_000;
/** After the deadline, a queued switch is still checked this often. */
export const CHANNEL_LATE_POLL_MS = 30_000;

export const REINSTALL_TEXT =
	'Nightly changes how your data is stored, so you cannot switch back to stable without reinstalling.';

/** One plain sentence for each refusal the channel API can give. */
export function channelError(cause: unknown): string {
	const status = (cause as { status?: unknown } | null)?.status;
	const detail = String((cause as { detail?: unknown } | null)?.detail ?? '');
	if (status === 403) return 'Only the owner can change the update channel, and not from a preview link.';
	if (status === 422) return 'This worker does not offer that update channel.';
	if (status === 409) {
		if (/reinstall|backwards/i.test(detail)) return REINSTALL_TEXT;
		if (/current update/i.test(detail)) return 'An update is in progress. Change the channel after it finishes.';
		if (/UPDATE_CHANNEL|launcher/i.test(detail)) return 'This worker’s environment or launcher sets its channel, so it cannot be changed here.';
		return 'This worker cannot change its update channel right now.';
	}
	return getErrorMessage(cause, 'Could not change the update channel');
}

/** Why the picker is read-only, or null when it can be changed. */
export function lockedReason(status: ChannelStatus | null): string | null {
	if (!status) return null;
	if (status.source === 'env') return 'Set by UPDATE_CHANNEL on this worker. Change it there and restart to switch.';
	if (!status.editable) return 'This worker’s launcher is offline or too old to switch channels. Update or reopen the app, then try again.';
	return null;
}

function timeoutError(): Error {
	return Object.assign(new Error('Status read timed out'), { name: 'AbortError' });
}

export class UpdateChannelState {
	status = $state<ChannelStatus | null>(null);
	load = $state<'loading' | 'ok' | 'forbidden' | 'error'>('loading');
	/** A channel the worker accepted (or was asked for) and has not confirmed yet. */
	target = $state<Channel | null>(null);
	/** The launcher did not confirm within CHANNEL_WAIT_MS; the request is still queued. */
	stalled = $state(false);
	/** The owner read the stalled warning; a quieter "still queued" line remains. */
	dismissed = $state(false);
	/** A PUT is in flight (first send or "Send again"). */
	sending = $state(false);
	/** A manual status check is running. */
	checking = $state(false);
	error = $state('');

	#alive = true;
	#run = 0;
	#poll: ReturnType<typeof setTimeout> | null = null;
	#deadline: ReturnType<typeof setTimeout> | null = null;
	#reads = new Set<AbortController>();

	/** The channel the picker shows: the one being applied, else the saved one. */
	get shown(): Channel {
		return this.target ?? this.status?.channel ?? 'stable';
	}

	/** Waiting for the launcher inside the deadline. */
	get busy(): boolean {
		return this.target !== null && !this.stalled;
	}

	/** Any switch the worker has not confirmed, before or after the deadline. */
	get pending(): boolean {
		return this.target !== null;
	}

	/** One GET that gives up after CHANNEL_READ_MS or when the wait is torn down. */
	async #read(): Promise<ChannelStatus> {
		const controller = new AbortController();
		this.#reads.add(controller);
		const timer = setTimeout(() => controller.abort(), CHANNEL_READ_MS);
		try {
			return await new Promise<ChannelStatus>((resolve, reject) => {
				controller.signal.addEventListener('abort', () => reject(timeoutError()), { once: true });
				api.get<ChannelStatus>('/updates/channel', { signal: controller.signal }).then(resolve, reject);
			});
		} finally {
			clearTimeout(timer);
			this.#reads.delete(controller);
		}
	}

	/** Takes a fresh status; ends a pending switch once the worker reports it. */
	#apply(next: ChannelStatus): void {
		this.status = next;
		if (this.target && next.channel === this.target) {
			this.#stop();
			this.target = null;
			this.stalled = false;
			this.dismissed = false;
			// A refused "Send again" no longer matters once the first request applied.
			this.error = '';
			flashSaved();
		}
	}

	async refresh(): Promise<void> {
		try {
			const next = await this.#read();
			if (!this.#alive) return;
			this.#apply(next);
			this.load = 'ok';
		} catch (cause) {
			if (!this.#alive) return;
			if (isForbidden(cause)) this.load = 'forbidden';
			else if (!this.status) {
				this.load = 'error';
				this.error = getErrorMessage(cause, 'Could not load the update channel');
			}
		}
	}

	/** "Check status": one read now, whatever the polling is doing. */
	async check(): Promise<void> {
		if (this.checking) return;
		this.checking = true;
		try {
			await this.refresh();
		} finally {
			this.checking = false;
		}
	}

	/** Saves `channel`; true once the worker reports it or has queued it. */
	async set(channel: Channel): Promise<boolean> {
		if (this.busy || this.sending || !this.status) return false;
		// "Send again" for a switch the worker still holds. A refusal must not
		// forget it: the earlier request can still apply, so its target and
		// background checks stay until the worker reports the channel.
		const resend = this.stalled && this.target === channel;
		let run = this.#run;
		if (!resend) {
			this.#stop();
			run = ++this.#run;
			this.target = channel;
			this.stalled = false;
			this.dismissed = false;
		}
		this.error = '';
		this.sending = true;
		let next: ChannelStatus;
		try {
			next = await api.put<ChannelStatus>('/updates/channel', { channel });
		} catch (cause) {
			if (!this.#alive || run !== this.#run) return false;
			if (!resend) this.target = null;
			this.error = channelError(cause);
			return false;
		} finally {
			this.sending = false;
		}
		if (!this.#alive || run !== this.#run) return false;
		// The earlier request was confirmed while this one was in flight.
		if (resend && this.target === null) return true;
		if (resend && next?.channel !== channel) {
			// Accepted again: start a fresh wait for the launcher.
			this.#stop();
			run = ++this.#run;
			this.stalled = false;
			this.dismissed = false;
		}
		if (next?.channel === channel) {
			this.#apply(next);
			return true;
		}
		// Desktop (202): queued; the launcher writes it on its next heartbeat.
		if (next) this.status = next;
		this.#deadline = setTimeout(() => {
			if (!this.#alive || run !== this.#run || !this.target) return;
			this.stalled = true;
			this.#abortReads();
			this.#schedule(run, CHANNEL_LATE_POLL_MS);
		}, CHANNEL_WAIT_MS);
		this.#schedule(run, CHANNEL_POLL_MS);
		return true;
	}

	#schedule(run: number, delay: number): void {
		if (this.#poll) clearTimeout(this.#poll);
		this.#poll = setTimeout(async () => {
			if (!this.#alive || run !== this.#run || !this.target) return;
			try {
				const fresh = await this.#read();
				if (!this.#alive || run !== this.#run) return;
				this.#apply(fresh);
			} catch {
				// A dropped, slow or aborted read: the next one tries again.
			}
			if (!this.#alive || run !== this.#run || !this.target) return;
			this.#schedule(run, this.stalled ? CHANNEL_LATE_POLL_MS : CHANNEL_POLL_MS);
		}, delay);
	}

	/** Hides the stalled warning. The switch stays queued and is still watched. */
	dismiss(): void {
		this.dismissed = true;
	}

	#abortReads(): void {
		for (const controller of this.#reads) controller.abort();
		this.#reads.clear();
	}

	#stop(): void {
		if (this.#poll) clearTimeout(this.#poll);
		if (this.#deadline) clearTimeout(this.#deadline);
		this.#poll = null;
		this.#deadline = null;
		this.#abortReads();
	}

	destroy(): void {
		this.#alive = false;
		++this.#run;
		this.#stop();
	}
}
