/**
 * Grace-period gate for the "connection lost" banner in ChatView.
 *
 * THE BUG THIS FIXES: the banner used to render immediately whenever
 * `activeWsConnected` went false. Switching tabs/panels (visibility changes,
 * brief WS blips during panel remounts, etc.) causes short-lived disconnects
 * that resolve within a second or two — but the banner still flashed on
 * every one of them, which reads as broken/flaky even though nothing is
 * actually wrong.
 *
 * The fix: only SHOW the banner once the disconnected state has persisted
 * continuously for `graceMs` (default 3000ms). The moment the connection
 * returns, hide immediately — no debounce on the way down, so the banner
 * never lingers after a real reconnect.
 *
 * Framework-free so it can be unit-tested with fake timers in isolation
 * (mirrors gap-indicator.ts / auto-scroll.ts). The component owns wiring
 * this to `$effect` + `onDestroy`; this module owns only the timing.
 */

export const DEFAULT_CONNECTION_BANNER_GRACE_MS = 3000;

export interface ConnectionBannerGraceConfig {
	/** Called once the disconnected state has persisted for the grace period. */
	onShow: () => void;
	/** Called the instant the connection returns while the banner is (or is about to be) shown. */
	onHide: () => void;
	/** Grace period in ms before the banner is shown. Defaults to 3000. */
	graceMs?: number;
}

export interface ConnectionBannerGrace {
	/**
	 * Report the current connection state. Call this from an `$effect` tracking
	 * the connection flag — safe to call repeatedly with the same value.
	 */
	setConnected(connected: boolean): void;
	/** Cancel any pending timer. Call on component destroy to avoid a leaked timer. */
	destroy(): void;
}

/**
 * Create a grace-period gate. `connected` starts assumed true (no timer
 * armed) — callers should invoke `setConnected` with the real initial state
 * immediately after creation if it might already be disconnected.
 */
export function createConnectionBannerGrace(config: ConnectionBannerGraceConfig): ConnectionBannerGrace {
	const graceMs = config.graceMs ?? DEFAULT_CONNECTION_BANNER_GRACE_MS;

	let timer: ReturnType<typeof setTimeout> | null = null;
	let shown = false;
	let destroyed = false;

	function clearTimer() {
		if (timer !== null) {
			clearTimeout(timer);
			timer = null;
		}
	}

	return {
		setConnected(connected: boolean) {
			if (destroyed) return;

			if (connected) {
				// Reconnected: cancel any pending grace timer and hide immediately —
				// no debounce on the way down.
				clearTimer();
				if (shown) {
					shown = false;
					config.onHide();
				}
				return;
			}

			// Disconnected: if already shown or a timer is already armed, nothing
			// to do — this call is idempotent for a continuing disconnect.
			if (shown || timer !== null) return;

			timer = setTimeout(() => {
				timer = null;
				shown = true;
				config.onShow();
			}, graceMs);
		},

		destroy() {
			destroyed = true;
			clearTimer();
		}
	};
}
