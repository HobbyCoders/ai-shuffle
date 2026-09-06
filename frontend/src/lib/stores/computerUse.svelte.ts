/**
 * Shared computer-use status store.
 *
 * Polls `/api/v1/computer-use/status` and exposes live state consumed by the
 * global toggle (Settings → Chat & Input) and the "AI Shuffle is controlling
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
import { getComputerUseStatus, setComputerUseEnabled, stopComputerUse, type ComputerUseStatus } from '$lib/api/computerUse';
import { notify } from '$lib/stores/notifications';

export const POLL_AVAILABLE_MS = 2_000;
export const POLL_UNAVAILABLE_MS = 60_000;

function pageHidden(): boolean {
	return typeof document !== 'undefined' && document.visibilityState === 'hidden';
}

class ComputerUseStore {
	available = $state(true);
	reason = $state('ok');
	enabled = $state(false);
	active = $state(false);
	display = $state<number | null>(null);
	displayLabel = $state<string | null>(null);
	action = $state<string | null>(null);
	busy = $state(false);
	cursor = $state<ComputerUseStatus['cursor']>(null);
	geometry = $state<ComputerUseStatus['geometry']>(null);
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
			this.enabled = s.enabled;
			this.active = s.active;
			this.display = s.display;
			this.displayLabel = s.display_label;
			this.action = s.action ?? null;
			this.busy = s.busy ?? false;
			this.cursor = s.cursor ?? null;
			this.geometry = s.geometry ?? null;
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
