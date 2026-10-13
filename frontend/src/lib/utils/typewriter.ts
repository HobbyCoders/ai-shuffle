/**
 * Typewriter animation controller.
 *
 * Reveals streaming content in word-sized chunks so a long response reads as
 * typing rather than as chunky poll bursts.
 *
 * Cost model (PERFORMANCE_REVIEW §3.2 R10): every emitted reveal length runs the
 * consumer's whole markdown pipeline — slice → `marked` on the tail paragraph →
 * DOMPurify → `{@html}` DOM replacement. The old loop emitted one reveal per
 * animation frame (~60/s), on top of the feed's own rAF folds, which doubled the
 * cost of streaming for a purely cosmetic effect. So:
 *
 *   - reveals are paced on a ~30 ms timer (≤33 renders/s, in practice fewer),
 *     and each step advances to a WORD boundary rather than one character;
 *   - the effect turns itself off entirely under `prefers-reduced-motion` and on
 *     low-core machines, where content is handed through unchanged the instant
 *     it arrives — no timer, no intermediate renders.
 *
 * Purely presentational — does not modify data pipelines. The component owns
 * reactive state ($state); this class manages the reveal cadence and reports the
 * current revealed-length via a callback.
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

export interface TypewriterOptions {
	/**
	 * Force the animation on/off. Defaults to `shouldAnimateText()` — i.e. off
	 * under reduced-motion and on low-end machines. Tests pass it explicitly.
	 */
	animate?: boolean;
	/** Reveal cadence in ms. Lower is smoother and more expensive. */
	chunkMs?: number;
}

/** Cores at or below this count: skip the animation and render content directly. */
export const LOW_CORE_THRESHOLD = 4;

/**
 * Whether this machine should get the typewriter at all.
 *
 * Reduced-motion is a stated preference and wins outright. The core count is a
 * capability heuristic: on a 2–4 core laptop the streaming pipeline is already
 * the frame budget, and a cosmetic re-render per word is exactly the work to
 * drop first. Unknown/absent hints are treated as capable.
 */
export function shouldAnimateText(): boolean {
	if (typeof window === 'undefined') return false;
	try {
		if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return false;
	} catch {
		// matchMedia unavailable (old jsdom): fall through to the core check.
	}
	const cores =
		typeof navigator !== 'undefined' && typeof navigator.hardwareConcurrency === 'number'
			? navigator.hardwareConcurrency
			: null;
	if (cores !== null && cores > 0 && cores <= LOW_CORE_THRESHOLD) return false;
	return true;
}

/**
 * Advance a reveal position to the end of the word it lands in.
 *
 * Emitting mid-word makes the tail paragraph re-flow on every step; snapping
 * forward to the next whitespace keeps each render a whole word and guarantees
 * progress (the position never moves backwards). The search is bounded so a very
 * long token — a URL, a base64 blob — cannot swallow the whole buffer in one
 * step and cannot stall the reveal either.
 */
export function snapToWordBoundary(text: string, from: number, maxLookahead = 24): number {
	if (from >= text.length) return text.length;
	const limit = Math.min(text.length, from + maxLookahead);
	for (let i = from; i < limit; i++) {
		if (/\s/.test(text[i])) return i + 1;
	}
	return limit;
}

export class Typewriter {
	/** Max ms the revealed text trails behind actual content */
	static readonly MAX_LATENCY_MS = 200;
	/** Skip animation if content jumps by more chars than this (reconnect / big load) */
	static readonly SKIP_CHARS = 500;
	/** Cap per-step dt to avoid huge jumps after tab-switch / debugger pause */
	static readonly MAX_DT_MS = 120;
	/** Default reveal cadence: ~33 renders/s worst case, usually far fewer. */
	static readonly CHUNK_MS = 30;
	/** Never emit a step shorter than this before the word-boundary snap. */
	static readonly MIN_CHARS_PER_STEP = 3;

	private _target = '';
	private _length = 0;
	private _timer: ReturnType<typeof setTimeout> | null = null;
	private _lastStep = 0;
	private _initialized = false;
	private readonly _onUpdate: (length: number) => void;
	private readonly _animate: boolean;
	private readonly _chunkMs: number;

	constructor(onUpdate: (length: number) => void, options: TypewriterOptions = {}) {
		this._onUpdate = onUpdate;
		this._animate = options.animate ?? shouldAnimateText();
		this._chunkMs = options.chunkMs ?? Typewriter.CHUNK_MS;
	}

	/** False when this instance passes content straight through (reduced motion / low-end). */
	get animating(): boolean {
		return this._animate;
	}

	/**
	 * Update target content and streaming state.
	 * Call from a reactive `$effect` whenever content or streaming changes.
	 */
	setTarget(text: string, streaming: boolean): void {
		this._target = text;

		if (!streaming) {
			this._emit(text.length);
			this._stop();
			this._initialized = false;
			return;
		}

		// Animation disabled for this machine: the revealed length is always the
		// full buffer, so the consumer renders exactly what arrived, once.
		if (!this._animate) {
			this._emit(text.length);
			return;
		}

		// First streaming call: show ALL existing content immediately.
		// Only animate content that arrives AFTER this point.
		// Fixes: component recreation mid-stream (Phase 3 ID transitions)
		// causing displayContent='' because _length starts at 0.
		if (!this._initialized) {
			this._initialized = true;
			this._emit(text.length);
			return;
		}

		// Clamp if content somehow shrank (defensive)
		if (this._length > text.length) {
			this._emit(text.length);
		}

		// Large jump (reconnect, initial load) — skip animation
		if (text.length - this._length > Typewriter.SKIP_CHARS) {
			this._emit(text.length);
			return;
		}

		// Start the reveal loop if not running and there's content to reveal
		if (this._timer === null && this._length < text.length) {
			this._lastStep = now();
			this._schedule();
		}
	}

	/** Clean up the pending step. Call on component destroy. */
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
		if (this._timer !== null) {
			clearTimeout(this._timer);
			this._timer = null;
		}
	}

	private _schedule(): void {
		this._timer = setTimeout(this._step, this._chunkMs);
	}

	/** Bound timer callback — arrow function for stable `this` reference */
	private _step = (): void => {
		this._timer = null;
		const stamp = now();
		const dt = Math.min(stamp - this._lastStep, Typewriter.MAX_DT_MS);
		this._lastStep = stamp;

		const remaining = this._target.length - this._length;
		if (remaining <= 0) return;

		// Adaptive rate: catch up within MAX_LATENCY_MS. A small buffer reveals a
		// word or so per step; a large one accelerates so the reveal never falls
		// visibly behind the stream.
		const catchup = Math.ceil((remaining / Typewriter.MAX_LATENCY_MS) * dt);
		const raw = Math.min(
			this._length + Math.max(Typewriter.MIN_CHARS_PER_STEP, catchup),
			this._target.length
		);
		const next = snapToWordBoundary(this._target, raw);

		this._emit(Math.min(next, this._target.length));
		if (this._length < this._target.length) this._schedule();
	};
}

function now(): number {
	return typeof performance !== 'undefined' && typeof performance.now === 'function'
		? performance.now()
		: Date.now();
}
