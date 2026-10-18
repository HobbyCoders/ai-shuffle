/**
 * Incremental left fold over a published transcript (#1282).
 *
 * Every streaming fold publishes a new messages array, but the reducer only
 * replaces the rows it touched (the tail, during a stream) and shares every
 * other message object. A forward scan whose state at row i depends only on
 * rows 0..i-1 can therefore resume from a checkpoint a few rows before the
 * tail once a pointer comparison shows the rows before it are the same
 * objects. Anything else (a rewind, a historical row replaced, a different
 * session) fails that check and rescans from the start, so the answer always
 * equals a full scan.
 *
 * `step` must be pure and return a new state rather than mutate the one it
 * was given: checkpoints keep references to earlier states.
 */
import type { ChatMessage } from '$lib/stores/types';

/** Rows behind the tail where the resume checkpoint sits. Streaming rewrites
 *  the last row or two; the margin keeps a burst of appends incremental. */
export const PREFIX_FOLD_TAIL = 16;

export function createPrefixFold<S>(
	initial: () => S,
	step: (state: S, message: ChatMessage, index: number) => S,
	tail = PREFIX_FOLD_TAIL
): (messages: readonly ChatMessage[]) => S {
	let previous: readonly ChatMessage[] | null = null;
	let result!: S;
	let checkpoint = 0;
	let checkpointState!: S;
	return (messages) => {
		if (messages === previous) return result;
		let start = 0;
		let state = initial();
		if (previous && checkpoint <= messages.length) {
			let same = true;
			for (let i = 0; i < checkpoint; i++) {
				if (messages[i] !== previous[i]) { same = false; break; }
			}
			if (same) { start = checkpoint; state = checkpointState; }
		}
		// Never behind `start`: the state there is the earliest one still known.
		const next = Math.max(start, messages.length - tail);
		for (let i = start; i < messages.length; i++) {
			if (i === next) checkpointState = state;
			state = step(state, messages[i], i);
		}
		if (next === messages.length) checkpointState = state;
		checkpoint = next;
		previous = messages;
		result = state;
		return state;
	};
}
