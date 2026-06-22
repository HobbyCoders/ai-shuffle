/**
 * Indicator-state derivation — single source of truth for the bottom-of-chat
 * activity indicator.
 *
 * Pure + framework-free so it can be unit-tested in isolation. The chat view
 * derives the indicator from `(isStreaming, activeTasks.length)`:
 *
 *   - streaming                         → 'dots'    (bouncing "..." — work in flight)
 *   - parked (!streaming) + tasks > 0   → 'waiting' (calm "Waiting for N task(s)…")
 *   - parked (!streaming) + no tasks    → 'none'    (no indicator at all)
 *
 * Bug 1: when a turn parks waiting on background tasks, the bouncing dots must
 * NOT persist — they made the UI look stuck. The 'waiting' state replaces them
 * with a distinct, calm indicator; the 'none' state ensures a finished, idle
 * turn shows nothing.
 */

export type IndicatorState = 'dots' | 'waiting' | 'none';

/**
 * Derive the activity-indicator state from streaming + active-task count.
 *
 * @param isStreaming   Whether the turn is actively streaming.
 * @param activeTaskCount Number of currently-running background tasks.
 */
export function deriveIndicatorState(
	isStreaming: boolean,
	activeTaskCount: number
): IndicatorState {
	if (isStreaming) return 'dots';
	if (activeTaskCount > 0) return 'waiting';
	return 'none';
}

/** Human-readable label for the parked "waiting" indicator. */
export function waitingLabel(activeTaskCount: number): string {
	const noun = activeTaskCount === 1 ? 'background task' : 'background tasks';
	return `Waiting for ${activeTaskCount} ${noun}…`;
}
