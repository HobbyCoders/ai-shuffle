// Checks over every painted frame of a recording (the flight recorder's
// timeline), for a case's `verify` hook. A screenshot or a poll can miss a
// state that lasted two frames; the timeline cannot.
//
// Timeline entries (browser-mcp/src/recorder/flight-recorder.js):
//   { t, kind: 'state', changes: { field: value | null } }   only what changed
//   { t, kind: 'mark', label } …
// Selector fields (recorder `selectors: ['stop=<css>']`) are element counts.

/**
 * The signature after every change, in order: [{ t, state }] where `state` is
 * the full field → value map at that moment (counts as numbers). The first
 * entry is the state when recording started.
 */
export function frames(recording) {
	const out = [];
	let state = {};
	for (const entry of recording?.timeline || []) {
		if (entry.kind !== 'state') continue;
		state = { ...state };
		for (const [field, value] of Object.entries(entry.changes || {})) {
			state[field] = value === null ? null : /^\d+$/.test(String(value)) ? Number(value) : String(value);
		}
		out.push({ t: entry.t, state });
	}
	return out;
}

/** The time of the last mark with this label (or the start when absent). */
export function markTime(recording, label) {
	const marks = (recording?.timeline || []).filter((e) => e.kind === 'mark' && e.label === label);
	return marks.length ? marks[marks.length - 1].t : 0;
}

/** The recording's end time. */
export function endTime(recording) {
	const timeline = recording?.timeline || [];
	return Math.max(recording?.durationMs || 0, ...timeline.map((e) => e.t || 0));
}

/**
 * Stretches where `predicate(state)` held: [{ start, end, ms }]. A stretch
 * still open when the recording ended runs to its end.
 */
export function spans(recording, predicate, { from = 0 } = {}) {
	const list = frames(recording);
	const end = endTime(recording);
	const out = [];
	let open = null;
	for (let i = 0; i < list.length; i++) {
		const { t, state } = list[i];
		const holds = t >= from && predicate(state);
		if (holds && open === null) open = t;
		if (!holds && open !== null) {
			out.push({ start: open, end: t, ms: t - open });
			open = null;
		}
	}
	if (open !== null) out.push({ start: open, end, ms: end - open });
	return out;
}

/** The final state of the recording. */
export function lastState(recording) {
	const list = frames(recording);
	return list.length ? list[list.length - 1].state : {};
}

/** Whether any frame from `from` on satisfied `predicate`. */
export function anyFrame(recording, predicate, { from = 0 } = {}) {
	return frames(recording).some(({ t, state }) => t >= from && predicate(state));
}

/**
 * The Stop button must follow the turn: shown while the reply streams (for a
 * turn that streams long enough to see it) and gone once it ends. Needs the
 * recorder selector `STOP_SELECTOR` (field `stop`).
 *
 *   - no stretch of `streaming=false` with Stop still shown for longer than
 *     `lingerMs` (a stuck Stop / "working" state);
 *   - every stretch of `streaming=true` of at least `visibleAfterMs` showed
 *     Stop at some frame;
 *   - the last frame shows no Stop and streaming=false.
 */
export function expectStopFollowsStreaming(recording, { from = 0, lingerMs = 800, visibleAfterMs = 400 } = {}) {
	const problems = [];
	for (const span of spans(recording, (s) => s.streaming === 'false' && Number(s.stop) > 0, { from })) {
		if (span.ms > lingerMs) problems.push(`Stop stayed on screen ${span.ms}ms after streaming ended (from ${span.start}ms)`);
	}
	for (const span of spans(recording, (s) => s.streaming === 'true', { from })) {
		if (span.ms < visibleAfterMs) continue;
		const shown = frames(recording).some(({ t, state }) => t >= span.start && t < span.end && Number(state.stop) > 0);
		if (!shown) problems.push(`no Stop button during a ${span.ms}ms reply (from ${span.start}ms)`);
	}
	const last = lastState(recording);
	if (Number(last.stop) > 0) problems.push('Stop is still shown at the end');
	if (last.streaming !== 'false') problems.push(`streaming is ${last.streaming} at the end`);
	if (problems.length) throw new Error(`Stop button out of step with the turn: ${problems.join('; ')}`);
}

/**
 * A count field (a recorder selector) may only grow from `from` on: a work log
 * or tool card that vanishes for a frame and comes back is a remount or a
 * reversal the mount markers do not cover.
 */
export function expectNeverDecreases(recording, field, { from = 0 } = {}) {
	let previous = null;
	for (const { t, state } of frames(recording)) {
		if (t < from) continue;
		const value = Number(state[field] ?? 0);
		if (previous !== null && value < previous.value) {
			throw new Error(`${field} went from ${previous.value} (at ${previous.t}ms) down to ${value} at ${t}ms`);
		}
		previous = { t, value };
	}
}

/** Recorder selector for the composer's Stop button (field `stop`). */
export const STOP_SELECTOR = 'stop=button[aria-label="Stop generating"]';
/** Recorder selector for the composer's Send button (field `send`). */
export const SEND_SELECTOR = 'send=button[aria-label="Send"]';
