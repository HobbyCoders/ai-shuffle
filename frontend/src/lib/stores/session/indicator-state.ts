/**
 * Indicator-state derivation — single source of truth for the bottom-of-chat
 * activity indicator.
 *
 * Pure + framework-free so it can be unit-tested in isolation. The chat view
 * derives the indicator from `(isStreaming, activeTasks.length)`. Active tasks
 * take PRIORITY over streaming:
 *
 *   - tasks > 0                         → 'waiting' (calm "Waiting for N task(s)…")
 *   - no tasks + streaming              → 'dots'    (bouncing "..." — work in flight)
 *   - no tasks + parked (!streaming)    → 'none'    (no indicator at all)
 *
 * WHY active-tasks-first: while a turn is PARKED waiting on a background
 * sub-agent, the SDK receive loop stays open, so the backend keeps reporting
 * `is_streaming = true` for the entire (multi-minute) run. Checking streaming
 * first would therefore show the bouncing "..." the whole time — the exact
 * "stuck" look of Bug 1. Prioritising activeTaskCount replaces it with the calm
 * 'waiting' state for as long as a task is live.
 *
 * The follow-up RESPONSE turn (after the parent "unparks") is unaffected: by
 * then the task is already completed, so activeTaskCount = 0 and the parent's
 * reply streams with 'dots' as normal. Ordinary no-task turns (activeTaskCount
 * = 0) are likewise unchanged — they fall straight through to the streaming
 * check.
 */

export type IndicatorState = 'dots' | 'waiting' | 'none';

/**
 * Derive the activity-indicator state from streaming + active-task count.
 *
 * Active tasks win: a running background task shows the calm 'waiting' state
 * even while the SDK loop still reports `isStreaming = true` (the park).
 *
 * @param isStreaming   Whether the turn is actively streaming.
 * @param activeTaskCount Number of currently-running background tasks.
 */
export function deriveIndicatorState(
	isStreaming: boolean,
	activeTaskCount: number
): IndicatorState {
	if (activeTaskCount > 0) return 'waiting';
	if (isStreaming) return 'dots';
	return 'none';
}

/** Human-readable label for the parked "waiting" indicator. */
export function waitingLabel(activeTaskCount: number): string {
	const noun = activeTaskCount === 1 ? 'background task' : 'background tasks';
	return `Waiting for ${activeTaskCount} ${noun}…`;
}
