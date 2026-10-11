/**
 * Deferred write of the transcript's reading position into session state.
 *
 * Writing `activeSession.scrollTop` on every scroll frame republished the whole
 * active-session object to all of its subscribers (#1282), so the write trails
 * scrolling by `delayMs`. Three rules keep that from losing or misfiling it:
 *
 * - The timer only writes while the session it was captured for is still the
 *   active one.
 * - Leaving a session writes its position under ITS OWN key (the ephemeral
 *   snapshot the switcher saved for it), never into the store, which by then
 *   already holds the session being entered.
 * - The position saved on leave is the one captured from the reader's last
 *   scroll, never the container's scrollTop read after the switch: by the time
 *   the switch effect runs, the DOM already holds the next session and a short
 *   transcript has clamped scrollTop (often to 0).
 */
export interface ScrollTopPersistDeps {
	delayMs: number;
	/** Key of the session on screen now ('new' for an unbound compose). */
	currentKey(): string;
	/** Write into the active session's state. */
	writeCurrent(position: number): void;
	/** Write into the saved snapshot of a session that is no longer active. */
	writeLeft(key: string, position: number): void;
}

export interface ScrollTopPersist {
	/** A scroll frame captured `position` for `key`; write it once scrolling pauses. */
	schedule(key: string, position: number): void;
	/**
	 * `key` was just switched away from. Saves the position last captured for
	 * it, else `known` (a position recorded before the switch), and returns the
	 * position saved — undefined when there was none, so nothing is written.
	 */
	leave(key: string, known?: number): number | undefined;
	/** The transcript is unmounting while `key` is on screen. */
	destroy(key: string, position: number): void;
	/** Stop a pending timer. The captured position is kept for `leave`. */
	cancel(): void;
}

export function createScrollTopPersist(deps: ScrollTopPersistDeps): ScrollTopPersist {
	let timer: ReturnType<typeof setTimeout> | null = null;
	let captured: { key: string; position: number } | null = null;

	function cancel() {
		if (timer !== null) clearTimeout(timer);
		timer = null;
	}

	return {
		schedule(key, position) {
			cancel();
			captured = { key, position };
			timer = setTimeout(() => {
				timer = null;
				if (deps.currentKey() === key) deps.writeCurrent(position);
			}, deps.delayMs);
		},
		leave(key, known) {
			cancel();
			const position = captured?.key === key ? captured.position : known;
			captured = null;
			if (position !== undefined) deps.writeLeft(key, position);
			return position;
		},
		destroy(key, position) {
			cancel();
			captured = null;
			if (deps.currentKey() === key) deps.writeCurrent(position);
		},
		cancel
	};
}
