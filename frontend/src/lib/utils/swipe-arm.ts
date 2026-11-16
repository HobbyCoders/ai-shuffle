/**
 * How far a finger drags a list row sideways before letting go commits.
 *
 * A share of the row's width suits a phone, but a row as wide as a foldable's
 * inner screen or a spanned dual-screen device would arm 300px or more in,
 * across most of the screen. The cap keeps the travel what a phone row asks
 * for: 40% of a 400px row, so phones up to about 428px wide (the phone
 * app's chat rows are the screen less 28px) keep the arm point they had.
 */
export const SWIPE_ARM_MAX_PX = 160;

/** The arm distance for a row `rowWidth` wide that arms at `ratio` of it. */
export function swipeArmDistance(rowWidth: number, ratio: number): number {
	return Math.min(rowWidth * ratio, SWIPE_ARM_MAX_PX);
}

/** A finger position along the gesture's axis and when it was there. */
export interface SwipeSample {
	t: number;
	x: number;
}

/** How far back the release speed looks: about three frames, so a burst of
 *  bunched moves averages out but the slow start of a flick does not count. */
export const FLICK_WINDOW_MS = 50;

/** A release this long after the last move was a hold, not a flick. */
export const FLICK_STALE_MS = 90;

/**
 * Whether an event's `timeStamp` is on the performance.now() clock. Browsers
 * stamp input events with when they happened on that clock; jsdom (and very
 * old browsers) stamp milliseconds since 1970, which no page lives long
 * enough to reach on the performance clock.
 */
function onPerformanceClock(timeStamp: number): boolean {
	return timeStamp > 0 && timeStamp < 1e12;
}

/**
 * Every position the browser coalesced into one pointermove (a busy page
 * hands a quick move over in a single event), else the event itself.
 */
export function coalescedMoves(event: PointerEvent): PointerEvent[] {
	const moves = typeof event.getCoalescedEvents === 'function' ? event.getCoalescedEvents() : [];
	return moves.length ? moves : [event];
}

/** When an event happened, on the one clock a gesture reads all its times by. */
export type GestureClock = (event?: Event) => number;

/**
 * The clock for a gesture that began with `press`: the events' own stamps
 * where the browser provides them, performance.now() otherwise (jsdom, or no
 * press). Picked once, so one gesture never combines the two. On the event
 * clock, an unstamped event mid-gesture counts as the latest stamped one.
 */
export function gestureClock(press?: Event): GestureClock {
	if (!press || !onPerformanceClock(press.timeStamp)) return () => performance.now();
	let latest = press.timeStamp;
	return (event) => (event && onPerformanceClock(event.timeStamp) ? (latest = event.timeStamp) : latest);
}

/**
 * The finger's speed (px/ms, signed) over the last `windowMs` of `samples`
 * (oldest first), not over the last move alone. With one sample in the window
 * it reaches back to the one before, so a slow drag reads slow. Two samples
 * at the same time read 0, never Infinity.
 */
export function flickVelocity(samples: readonly SwipeSample[], windowMs = FLICK_WINDOW_MS): number {
	if (samples.length < 2) return 0;
	const last = samples[samples.length - 1];
	let first = samples.length - 2;
	while (first > 0 && last.t - samples[first - 1].t <= windowMs) first--;
	const from = samples[first];
	const elapsed = last.t - from.t;
	return elapsed > 0 ? (last.x - from.x) / elapsed : 0;
}

/**
 * A finger's release speed along one axis, for every swipe and drag that a
 * flick can finish (#1637). Shared so each gesture reads speed the same way:
 *
 * - By when each move happened (the event's own stamp), not when the page got
 *   to it: a busy page handles moves late and in bunches, and handler time
 *   made one move's speed swing from a fraction of the finger's to several
 *   times it.
 * - Including the moves the browser coalesced into one pointermove, so a
 *   whole flick handed over at once still reads its own speed.
 * - Over the last `windowMs`, not the last move alone.
 * - Zero when the finger rested `staleMs` before letting go: a fast move, a
 *   hold, then a lift is not a flick.
 *
 * The press picks one clock for the whole gesture: event stamps where the
 * browser provides them, performance.now() otherwise (jsdom). The two are
 * never combined.
 */
export class FlickTracker {
	private samples: SwipeSample[] = [];
	private clock: GestureClock = gestureClock();
	private readonly windowMs: number;
	private readonly staleMs: number;

	constructor({ windowMs = FLICK_WINDOW_MS, staleMs = FLICK_STALE_MS }: { windowMs?: number; staleMs?: number } = {}) {
		this.windowMs = windowMs;
		this.staleMs = staleMs;
	}

	/** Begin at the press: `pos` along the axis, `event` the press. */
	start(pos: number, event?: Event): void {
		this.clock = gestureClock(event);
		this.samples = [];
		this.add(pos, event);
	}

	/** When `event` happened on this gesture's clock. */
	time(event?: Event): number {
		return this.clock(event);
	}

	/** One position, at `event`'s time. */
	add(pos: number, event?: Event): void {
		this.addAt(pos, this.time(event));
	}

	/**
	 * A pointermove: every position the browser coalesced into it, else the
	 * event's own. `axis` picks clientX or clientY.
	 */
	addPointer(event: PointerEvent, axis: 'x' | 'y' = 'x'): void {
		for (const move of coalescedMoves(event)) this.add(axis === 'x' ? move.clientX : move.clientY, move);
	}

	/**
	 * One position at time `t`, for a caller that keeps its own clock (one
	 * clock for the whole gesture, release included). Moves earlier than the
	 * latest are dropped; a move at the same time replaces its position.
	 */
	addAt(pos: number, t: number): void {
		const last = this.samples.at(-1);
		if (last && t < last.t) return; // Out of order: keep time moving forward.
		if (last && t === last.t) { last.x = pos; return; } // Same instant: the later position wins.
		this.samples.push({ t, x: pos });
		// Keep one sample older than the window, so a slow drag reads slow.
		while (this.samples.length > 2 && t - this.samples[1].t > this.windowMs) this.samples.shift();
	}

	/**
	 * Speed (px/ms, signed) at the latest move, or at letting go when given
	 * the release event: zero if the finger rested past `staleMs` first.
	 */
	velocity(release?: Event): number {
		return this.velocityAt(release ? this.time(release) : undefined);
	}

	/** {@link velocity} for a caller with its own clock: `releaseTime` on it. */
	velocityAt(releaseTime?: number): number {
		const last = this.samples.at(-1);
		if (!last) return 0;
		if (releaseTime !== undefined && releaseTime - last.t > this.staleMs) return 0;
		return flickVelocity(this.samples, this.windowMs);
	}
}
