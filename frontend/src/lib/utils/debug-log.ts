/**
 * Streaming/diagnostics debug gate.
 *
 * The chat streaming pipeline used to emit verbose `console.log` diagnostics on
 * every poll (150ms), every WS delta, and every scroll frame. Those logs build
 * O(N) strings from the message array on each call and — with DevTools open —
 * accumulate retained objects in the console buffer, which is a primary cause of
 * the app getting progressively slower (and eventually stalling) over long
 * sessions.
 *
 * Hot-path diagnostics are now guarded by this flag, which is OFF by default and
 * short-circuits before any string is built (zero cost when disabled). To
 * re-enable while debugging, run in the browser console:
 *
 *   localStorage.setItem('AIHUB_DEBUG_STREAM', '1'); location.reload();
 *
 * and to disable again:
 *
 *   localStorage.removeItem('AIHUB_DEBUG_STREAM'); location.reload();
 */

function readFlag(): boolean {
	try {
		return typeof localStorage !== 'undefined'
			&& localStorage.getItem('AIHUB_DEBUG_STREAM') === '1';
	} catch {
		// localStorage can throw in sandboxed/SSR contexts — default to off.
		return false;
	}
}

/**
 * Evaluated once at module load. Diagnostics are a debug aid, not runtime
 * behavior, so a reload-to-toggle gate keeps hot paths branch-predictable and
 * avoids a localStorage read on every poll/delta/frame.
 */
export const STREAM_DEBUG: boolean = readFlag();
