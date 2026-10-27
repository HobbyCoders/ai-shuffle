/**
 * Grace-period gate for the "connection lost" notices (ChatView's reconnect
 * pill and banner, MirrorSyncGate's sync pill).
 *
 * THE BUG THIS FIXES: a notice that renders the instant the connection
 * drops flashes on every short blip (switching tabs or panels, a brief WS
 * hiccup during a remount) even though nothing is wrong. So the notice only
 * SHOWS once the disconnect has lasted `graceMs` (default 3000ms) and hides
 * the moment the connection returns, with no debounce on the way down.
 *
 * ONLY VISIBLE TIME COUNTS (default; `visibleTimeOnly: false` opts out).
 * A phone kills the socket while the app is in the background. With a plain
 * timer the grace period ran out in the background (or fired as an overdue
 * timer the moment the WebView woke), so the notice flashed on every resume
 * while the immediate resume reconnect was still handshaking. Now:
 * - while `document.hidden`, nothing is shown and no timer runs; hiding the
 *   page hides a shown notice;
 * - when the page becomes visible and is still disconnected, a FRESH grace
 *   window starts;
 * - a timer that fires while the page is hidden does nothing, and one that
 *   fires much later than scheduled (the page was frozen without a
 *   `visibilitychange`, and the timer ran on wake) re-arms a fresh window
 *   instead of showing.
 *
 * Framework-free so it can be unit-tested with fake timers in isolation
 * (mirrors gap-indicator.ts / auto-scroll.ts). The component owns wiring
 * this to `$effect` + `onDestroy`; this module owns only the timing.
 */

export const DEFAULT_CONNECTION_BANNER_GRACE_MS = 3000;

/** A timer firing this much past its due time slept through a frozen page. */
export const OVERDUE_TIMER_SLACK_MS = 1000;

export interface ConnectionBannerGraceConfig {
	/** Called once the disconnected state has persisted for the grace period. */
	onShow: () => void;
	/** Called the instant the connection returns (or the page hides) while the notice is shown. */
	onHide: () => void;
	/** Grace period in ms before the notice is shown. Defaults to 3000. */
	graceMs?: number;
	/**
	 * Count only time the page is visible (default true). Background time
	 * never runs the grace period down; each return to the page starts a
	 * fresh window. No effect where there is no `document`.
	 */
	visibleTimeOnly?: boolean;
	/** Clock for overdue-timer detection. Defaults to `Date.now`; tests inject a skewed clock. */
	now?: () => number;
}

export interface ConnectionBannerGrace {
	/**
	 * Report the current connection state. Call this from an `$effect` tracking
	 * the connection flag — safe to call repeatedly with the same value.
	 */
	setConnected(connected: boolean): void;
	/** Cancel any pending timer and stop listening. Call on component destroy. */
	destroy(): void;
}

/**
 * Create a grace-period gate. `connected` starts assumed true (no timer
 * armed) — callers should invoke `setConnected` with the real initial state
 * immediately after creation if it might already be disconnected.
 */
export function createConnectionBannerGrace(config: ConnectionBannerGraceConfig): ConnectionBannerGrace {
	const graceMs = config.graceMs ?? DEFAULT_CONNECTION_BANNER_GRACE_MS;
	const now = config.now ?? Date.now;
	const doc = (config.visibleTimeOnly ?? true) && typeof document !== 'undefined' ? document : null;

	let timer: ReturnType<typeof setTimeout> | null = null;
	let dueAt = 0;
	let connected = true;
	let shown = false;
	let destroyed = false;

	const pageHidden = () => doc?.hidden === true;

	function clearTimer() {
		if (timer !== null) {
			clearTimeout(timer);
			timer = null;
		}
	}

	function hide() {
		clearTimer();
		if (shown) {
			shown = false;
			config.onHide();
		}
	}

	function arm() {
		clearTimer();
		dueAt = now() + graceMs;
		timer = setTimeout(fire, graceMs);
	}

	function fire() {
		timer = null;
		if (destroyed || connected || shown) return;
		// Fired while hidden: wait for the page to come back (a fresh window
		// starts on the visible event).
		if (pageHidden()) return;
		// Fired long after it was due: the page was frozen and this timer ran
		// on wake, possibly before `visibilitychange`. The frozen time doesn't
		// count, so give the resume reconnect a full window.
		if (doc && now() - dueAt > OVERDUE_TIMER_SLACK_MS) {
			arm();
			return;
		}
		shown = true;
		config.onShow();
	}

	function onVisibilityChange() {
		if (destroyed) return;
		if (pageHidden()) {
			hide();
			return;
		}
		// Back on screen and still disconnected: fresh window. A timer already
		// armed since the wake (or a duplicate event) is left alone.
		if (!connected && !shown && timer === null) arm();
	}

	if (doc) {
		doc.addEventListener('visibilitychange', onVisibilityChange);
		// BFCache restores (mobile Safari unlock) may skip visibilitychange.
		window.addEventListener('pageshow', onVisibilityChange);
	}

	return {
		setConnected(next: boolean) {
			if (destroyed) return;
			connected = next;

			if (next) {
				// Reconnected: cancel any pending timer and hide immediately.
				hide();
				return;
			}

			// Disconnected: idempotent for a continuing disconnect. While hidden
			// nothing arms; the visible event starts the window.
			if (shown || timer !== null || pageHidden()) return;
			arm();
		},

		destroy() {
			destroyed = true;
			clearTimer();
			if (doc) {
				doc.removeEventListener('visibilitychange', onVisibilityChange);
				window.removeEventListener('pageshow', onVisibilityChange);
			}
		}
	};
}
