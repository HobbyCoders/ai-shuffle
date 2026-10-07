/**
 * Shared computer-use status store.
 *
 * Polls `/api/v1/computer-use/status` and exposes live state consumed by the
 * global toggle (Settings > Security) and the "AI Shuffle is controlling
 * your screen" toast. There is exactly ONE poller per app instance: the
 * store is a module singleton and `start()`/`stop()` are ref-counted, so any
 * number of session views share a single timer.
 *
 * Cadence is adaptive. While the capability is available the status is
 * refreshed every `POLL_AVAILABLE_MS` (2 s — the banner must react quickly when
 * the agent takes the screen). When the server says computer use is
 * unavailable there is nothing to react to, so the poll backs off to
 * `POLL_UNAVAILABLE_MS` (60 s) until it comes back. Polling pauses entirely
 * while the page is hidden (`document.visibilityState`) and resumes — with an
 * immediate refresh — when it becomes visible again. In the 2026-08-22 incident
 * this endpoint was hit 78 times in ten minutes from a single device while the
 * backend was already struggling, every call slow.
 */
import {
	getComputerUseStatus,
	setComputerUseEnabled,
	setComputerUseInputMode,
	stopComputerUse,
	type ComputerUseInputMode,
	type ComputerUseStatus
} from '$lib/api/computerUse';
import { notify } from '$lib/stores/notifications';

export const POLL_AVAILABLE_MS = 2_000;
export const POLL_UNAVAILABLE_MS = 60_000;

/** Interrupt sources that are a person's chat Stop (backend abort._USER_SOURCES). */
const USER_STOP_SOURCES = new Set(['user_stop', 'user_stopped', 'user_stopped_all']);

/** What the app was doing when it interrupted the turn (backend abort._APP_SOURCES). */
const APP_STOP_SOURCES: Record<string, string> = {
	server_shutdown: 'The app is shutting down.',
	worker_update: 'This worker is updating.',
	session_handoff: 'The chat is moving to another worker.',
	app_stop: 'The app stopped the chat.',
	checkout_cleanup: "The chat's checkout was removed.",
	assistant: 'An assistant limit stopped the run.',
	automation_timeout: 'The automation hit its time limit.',
	lease_lost: 'This worker lost ownership of the turn.',
	fence_superseded: 'Another worker took over the turn.'
};

export interface StopNotice {
	title: string;
	detail: string;
}

/**
 * Why control ended, for the banner, from the status endpoint's
 * `stop_reason`. Only a person's Esc or Stop is described as theirs; an app
 * interrupt says what the app did. Null when there is nothing to report.
 */
export function stopNotice(reason: string | null | undefined): StopNotice | null {
	if (!reason) return null;
	const stopped = 'Computer use stopped';
	const ended = 'Computer use ended';
	if (reason === 'escape') return { title: stopped, detail: 'Esc was pressed on this computer. You have control.' };
	if (reason === 'user') return { title: stopped, detail: 'Stopped with Stop computer use. You have control.' };
	if (reason === 'disabled') return { title: stopped, detail: 'Computer use was turned off in Settings.' };
	if (reason === 'completed') return { title: ended, detail: 'The turn finished. You have control.' };
	if (reason.startsWith('interrupt:')) {
		const source = reason.slice('interrupt:'.length);
		if (USER_STOP_SOURCES.has(source)) {
			return { title: stopped, detail: 'Stopped with Stop in the chat. You have control.' };
		}
		return { title: ended, detail: `${APP_STOP_SOURCES[source] ?? 'The app interrupted the turn.'} You have control.` };
	}
	// "stopped": a turn interrupt whose source was not recorded.
	return { title: ended, detail: 'The chat turn was stopped. You have control.' };
}

function pageHidden(): boolean {
	return typeof document !== 'undefined' && document.visibilityState === 'hidden';
}

class ComputerUseStore {
	available = $state(true);
	reason = $state('ok');
	/** Why it is unavailable and what to do, from the server (null when available). */
	detail = $state<string | null>(null);
	enabled = $state(false);
	inputMode = $state<ComputerUseInputMode>('shared_desktop');
	active = $state(false);
	display = $state<number | null>(null);
	displayLabel = $state<string | null>(null);
	action = $state<string | null>(null);
	busy = $state(false);
	cursor = $state<ComputerUseStatus['cursor']>(null);
	geometry = $state<ComputerUseStatus['geometry']>(null);
	apps = $state<string[]>([]);
	secondsSince = $state<number | null>(null);
	stopReason = $state<string | null>(null);
	escapeAvailable = $state(false);
	overlayAvailable = $state(false);
	stopping = $state(false);
	connectionLost = $state(false);
	#revision = 0;

	#timer: ReturnType<typeof setTimeout> | null = null;
	#subscribers = 0;
	#wasActive = false;
	#polling = false;
	#inFlight = false;
	#visibilityHandler: (() => void) | null = null;

	/** Current poll interval, derived from the last known availability. */
	get pollIntervalMs(): number {
		return this.available ? POLL_AVAILABLE_MS : POLL_UNAVAILABLE_MS;
	}

	/** Whether a poll loop is currently armed (for tests / diagnostics). */
	get polling(): boolean {
		return this.#polling;
	}

	async refresh() {
		// One request in flight at a time: a slow backend must not let the
		// timer stack requests on top of each other.
		if (this.#inFlight) return;
		this.#inFlight = true;
		const revision = this.#revision;
		try {
			const s = await getComputerUseStatus();
			if (revision !== this.#revision) return;
			this.connectionLost = false;
			this.available = s.available;
			this.reason = s.reason;
			this.detail = s.detail ?? null;
			this.enabled = s.enabled;
			this.inputMode = s.input_mode === 'dedicated_desktop' ? 'dedicated_desktop' : 'shared_desktop';
			this.active = s.active;
			this.display = s.display;
			this.displayLabel = s.display_label;
			this.action = s.action ?? null;
			this.busy = s.busy ?? false;
			this.cursor = s.cursor ?? null;
			this.geometry = s.geometry ?? null;
			this.apps = s.apps ?? [];
			this.secondsSince = s.seconds_since ?? null;
			this.stopReason = s.stop_reason ?? null;
			this.escapeAvailable = s.escape_available ?? false;
			this.overlayAvailable = s.overlay_available ?? false;
			// Toast on the rising edge of control activity, so the user is told
			// the moment the agent starts driving a screen (and which one).
			if (s.active && !this.#wasActive) {
				notify(`AI Shuffle is controlling ${s.display_label ?? 'your screen'}`, {
					severity: 'info',
					duration_ms: 4000
				});
			}
			this.#wasActive = s.active;
		} catch {
			if (revision === this.#revision) this.connectionLost = true;
		} finally {
			this.#inFlight = false;
		}
	}

	/** Release host input immediately; don't wait for the next status poll. */
	async halt() {
		if (this.stopping) return;
		this.stopping = true;
		this.#revision++;
		try {
			await stopComputerUse();
			this.#revision++;
			this.active = false;
			this.#wasActive = false;
			this.stopReason = 'user';
			this.cursor = null;
			this.geometry = null;
			this.apps = [];
			this.display = null;
			this.displayLabel = null;
		} catch {
			notify('Could not stop computer use. Press Esc on the host computer or use Stop in chat.', {
				severity: 'error', duration_ms: 8000
			});
		} finally {
			this.stopping = false;
		}
	}

	/** Begin polling. Ref-counted: multiple components share one loop. */
	start() {
		this.#subscribers++;
		if (this.#polling) return;
		this.#polling = true;
		this.#installVisibilityListener();
		void this.#tick();
	}

	/** Stop polling when the last consumer unmounts. */
	stop() {
		this.#subscribers = Math.max(0, this.#subscribers - 1);
		if (this.#subscribers > 0) return;
		this.#polling = false;
		this.#clearTimer();
		this.#removeVisibilityListener();
	}

	/** Set the global computer-use toggle (persisted server-side). */
	async setEnabled(on: boolean) {
		const prev = this.enabled;
		// Optimistic flip; rolled back below if the server rejects it.
		this.enabled = on;
		try {
			const res = await setComputerUseEnabled(on);
			this.enabled = res.enabled;
		} catch (e) {
			console.warn('computer-use toggle failed', e);
			this.enabled = prev;
		}
		await this.refresh();
	}

	/** Say whether only agents use this computer (persisted server-side). */
	async setInputMode(mode: ComputerUseInputMode) {
		const prev = this.inputMode;
		// A poll already in flight carries the old mode; drop its answer.
		this.#revision++;
		this.inputMode = mode;
		try {
			const res = await setComputerUseInputMode(mode);
			this.inputMode = res.input_mode;
		} catch (e) {
			console.warn('computer-use input mode change failed', e);
			this.inputMode = prev;
		}
		await this.refresh();
	}

	// ── poll loop ──────────────────────────────────────────────────────

	async #tick() {
		if (!this.#polling) return;
		if (pageHidden()) {
			// Nothing to show in a hidden tab; the visibility listener restarts
			// the loop when the page is shown again.
			this.#clearTimer();
			return;
		}
		await this.refresh();
		this.#schedule();
	}

	#schedule() {
		this.#clearTimer();
		if (!this.#polling || pageHidden()) return;
		this.#timer = setTimeout(() => {
			this.#timer = null;
			void this.#tick();
		}, this.pollIntervalMs);
	}

	#clearTimer() {
		if (this.#timer !== null) {
			clearTimeout(this.#timer);
			this.#timer = null;
		}
	}

	#installVisibilityListener() {
		if (typeof document === 'undefined' || this.#visibilityHandler) return;
		this.#visibilityHandler = () => {
			if (!this.#polling) return;
			if (pageHidden()) {
				this.#clearTimer();
			} else if (this.#timer === null) {
				// Came back to the foreground with no timer armed: refresh now
				// rather than waiting out a (possibly 60 s) interval.
				void this.#tick();
			}
		};
		document.addEventListener('visibilitychange', this.#visibilityHandler);
	}

	#removeVisibilityListener() {
		if (typeof document === 'undefined' || !this.#visibilityHandler) return;
		document.removeEventListener('visibilitychange', this.#visibilityHandler);
		this.#visibilityHandler = null;
	}
}

export const computerUse = new ComputerUseStore();
