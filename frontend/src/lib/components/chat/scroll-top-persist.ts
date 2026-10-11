/**
 * Deferred write of the transcript's reading position into session state.
 *
 * Writing `activeSession.scrollTop` on every scroll frame republished the whole
 * active-session object to all of its subscribers (#1282), so the write trails
 * scrolling by `delayMs`. Two rules keep that from losing or misfiling it:
 *
 * - The timer only writes while the session it was captured for is still the
 *   active one.
 * - Leaving a session writes its position under ITS OWN key (the ephemeral
 *   snapshot the switcher saved for it), never into the store, which by then
 *   already holds the session being entered.
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
	/** `key` was just switched away from with its last `position`. */
	leave(key: string, position: number): void;
	/** The transcript is unmounting while `key` is on screen. */
	destroy(key: string, position: number): void;
	cancel(): void;
}

export function createScrollTopPersist(deps: ScrollTopPersistDeps): ScrollTopPersist {
	let timer: ReturnType<typeof setTimeout> | null = null;

	function cancel() {
		if (timer !== null) clearTimeout(timer);
		timer = null;
	}

	return {
		schedule(key, position) {
			cancel();
			timer = setTimeout(() => {
				timer = null;
				if (deps.currentKey() === key) deps.writeCurrent(position);
			}, deps.delayMs);
		},
		leave(key, position) {
			cancel();
			deps.writeLeft(key, position);
		},
		destroy(key, position) {
			cancel();
			if (deps.currentKey() === key) deps.writeCurrent(position);
		},
		cancel
	};
}
