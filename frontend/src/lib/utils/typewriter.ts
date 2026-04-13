/**
 * Typewriter animation controller.
 *
 * Smoothly reveals streaming content character-by-character via
 * requestAnimationFrame instead of showing chunky poll bursts.
 *
 * Purely presentational — does not modify data pipelines. The component
 * owns reactive state ($state); this class manages the rAF loop and
 * reports the current revealed-length via a callback.
 *
 * Usage (in a Svelte 5 component):
 * ```ts
 * let twLength = $state(0);
 * const tw = new Typewriter((len) => { twLength = len; });
 *
 * $effect(() => tw.setTarget(content || '', streaming));
 * onDestroy(() => tw.destroy());
 *
 * const display = $derived(
 *   streaming && twLength < (content || '').length
 *     ? (content || '').slice(0, twLength)
 *     : (content || '')
 * );
 * ```
 */
export class Typewriter {
	/** Max ms the revealed text trails behind actual content */
	static readonly MAX_LATENCY_MS = 200;
	/** Skip animation if content jumps by more chars than this (reconnect / big load) */
	static readonly SKIP_CHARS = 500;
	/** Cap per-frame dt to avoid huge jumps after tab-switch / debugger pause */
	static readonly MAX_DT_MS = 50;

	private _target = '';
	private _length = 0;
	private _rafId: number | null = null;
	private _lastFrame = 0;
	private _initialized = false;
	private readonly _onUpdate: (length: number) => void;

	constructor(onUpdate: (length: number) => void) {
		this._onUpdate = onUpdate;
	}

	/**
	 * Update target content and streaming state.
	 * Call from a reactive `$effect` whenever content or streaming changes.
	 */
	setTarget(text: string, streaming: boolean): void {
		if (!streaming) {
			this._target = text;
			this._emit(text.length);
			this._stop();
			this._initialized = false;
			return;
		}

		// First streaming call: show ALL existing content immediately.
		// Only animate content that arrives AFTER this point.
		// Fixes: component recreation mid-stream (Phase 3 ID transitions)
		// causing displayContent='' because _length starts at 0.
		if (!this._initialized) {
			this._initialized = true;
			this._target = text;
			this._emit(text.length);
			return;
		}

		this._target = text;

		// Clamp if content somehow shrank (defensive)
		if (this._length > text.length) {
			this._emit(text.length);
		}

		// Large jump (reconnect, initial load) — skip animation
		if (text.length - this._length > Typewriter.SKIP_CHARS) {
			this._emit(text.length);
			return;
		}

		// Start rAF loop if not running and there's content to reveal
		if (this._rafId === null && this._length < text.length) {
			this._lastFrame = performance.now();
			this._rafId = requestAnimationFrame(this._tick);
		}
	}

	/** Clean up rAF. Call on component destroy. */
	destroy(): void {
		this._stop();
	}

	// ── internals ──────────────────────────────────────────────────────

	private _emit(length: number): void {
		if (length !== this._length) {
			this._length = length;
			this._onUpdate(length);
		}
	}

	private _stop(): void {
		if (this._rafId !== null) {
			cancelAnimationFrame(this._rafId);
			this._rafId = null;
		}
	}

	/** Bound rAF callback — arrow function for stable `this` reference */
	private _tick = (now: number): void => {
		const dt = Math.min(now - this._lastFrame, Typewriter.MAX_DT_MS);
		this._lastFrame = now;

		const remaining = this._target.length - this._length;
		if (remaining <= 0) {
			this._rafId = null;
			return;
		}

		// Adaptive rate: catch up within MAX_LATENCY_MS.
		// Small buffer → 1 char/frame (smooth typing).
		// Large buffer → fast catch-up to prevent falling behind.
		const catchup = Math.ceil((remaining / Typewriter.MAX_LATENCY_MS) * dt);
		const chars = Math.max(1, catchup);

		this._emit(Math.min(this._length + chars, this._target.length));
		this._rafId = requestAnimationFrame(this._tick);
	};
}
