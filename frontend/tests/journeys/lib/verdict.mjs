// How the journey runner judges a case, as pure functions (self-tested by
// verdict.test.mjs; the runner wires them to Playwright).
//
//   designedShifts()  the narrowly-scoped exception for a layout move the
//                     product makes on purpose without user input (a cleared
//                     chat's composer moving to the centre when it opens by
//                     link or is cleared by the server). Never a blanket allow.
//   classifyKnownBug() whether a failing case marked `knownBug` may be reported
//                     as KNOWN BUG instead of FAIL.
import { detectLayoutShift } from '../../../../browser-mcp/src/recorder/analyze.js';
import { frames } from './timeline.mjs';

/** How far from a designed composer move (by its painted frame) a layout shift
 *  still belongs to it: the shift is reported for the frame that moved, which
 *  can be stamped a frame or two either side of the sampled signature. */
export const DESIGNED_SHIFT_WINDOW_MS = { before: 150, after: 400 };

/** The chat layout a composer move shifts: ChatView's own elements (the input
 *  slot, the hero, its content). Chromium reports one entry per frame for
 *  everything that moved in it, so an entry is the move's when the chat
 *  layout is among its sources (the header title settling in the same frame
 *  rides along); the step, the window, the single move and `maxValue` bound
 *  what that can cover. */
export const COMPOSER_LAYOUT_SOURCE = /\sin ChatView$/;

/**
 * Check a case's `expectShift` declarations against a recording.
 *
 * expectShift: [{
 *   step:     the mark label of the step the move belongs to,
 *   composer: [from, to], the one data-ui-composer change the step designs,
 *   maxValue: the largest total layout shift that move may measure,
 *   reason:   why the product moves it without user input,
 * }]
 *
 * Each declaration must find EXACTLY one composer change from → to inside its
 * step (between its mark and the next one); none, or two, is a failure. Only
 * the layout-shift entries within DESIGNED_SHIFT_WINDOW_MS of that change that
 * moved the chat layout (COMPOSER_LAYOUT_SOURCE) are excused, and only up to `maxValue`
 * in total: a larger move, a second move, a shift anywhere else or of anything
 * else still counts. The layout_shift anomaly is then recomputed without the
 * excused entries (same detector, same thresholds).
 *
 * `partial` is for the recording of a case that failed part-way: a step it
 * never reached, or the step it failed in before the move, owes no move.
 *
 * Returns { anomalies, excused: [{ step, value, t }], problems: [string] }:
 * `anomalies` replaces the recording's own list; `problems` fail the case.
 */
export function designedShifts(recording, anomalies, expectShift = [], { partial = false } = {}) {
	if (!expectShift.length) return { anomalies, excused: [], problems: [] };
	const timeline = recording?.timeline || [];
	const marks = timeline.filter((e) => e.kind === 'mark');
	const states = frames(recording);
	const problems = [];
	const excusedIndices = new Set();
	const excused = [];
	for (const design of expectShift) {
		const markIndex = marks.findIndex((m) => m.label === design.step);
		if (markIndex < 0) {
			if (!partial) problems.push(`expectShift: no step marked "${design.step}"`);
			continue;
		}
		const from = marks[markIndex].t;
		const to = markIndex + 1 < marks.length ? marks[markIndex + 1].t : Infinity;
		const [before, after] = design.composer;
		const moves = [];
		for (let i = 1; i < states.length; i++) {
			const { t, state } = states[i];
			const previous = states[i - 1].state.composer;
			if (t < from || t >= to || state.composer === previous) continue;
			if (previous === before && state.composer === after) moves.push(t);
		}
		if (partial && moves.length === 0 && markIndex === marks.length - 1) continue;
		if (moves.length !== 1) {
			problems.push(`expectShift "${design.step}": the composer moved ${before} → ${after} ${moves.length} times in that step, not once`);
			continue;
		}
		const at = moves[0];
		let total = 0;
		const own = [];
		timeline.forEach((entry, index) => {
			if (entry.kind !== 'layout_shift' || entry.input || typeof entry.value !== 'number') return;
			if (entry.t < at - DESIGNED_SHIFT_WINDOW_MS.before || entry.t > at + DESIGNED_SHIFT_WINDOW_MS.after) return;
			const sources = Array.isArray(entry.sources) ? entry.sources : [];
			if (!sources.some((s) => COMPOSER_LAYOUT_SOURCE.test(s))) return;
			own.push(index);
			total += entry.value;
		});
		if (total > design.maxValue) {
			// Too big to be the designed move: excuse none of it.
			problems.push(`expectShift "${design.step}": the designed composer move measured ${total.toFixed(3)}, more than its ${design.maxValue}`);
			continue;
		}
		for (const index of own) excusedIndices.add(index);
		excused.push({ step: design.step, value: Number(total.toFixed(4)), t: at, reason: design.reason });
	}
	if (!excusedIndices.size) return { anomalies, excused, problems };
	const excusedTotal = [...excusedIndices].reduce((sum, i) => sum + timeline[i].value, 0);
	const stats = recording.stats?.layoutShift;
	const rest = {
		...recording,
		timeline: timeline.map((entry, index) => (excusedIndices.has(index) ? { ...entry, kind: 'excused_layout_shift' } : entry)),
		stats: {
			...recording.stats,
			...(stats && typeof stats.total === 'number'
				? { layoutShift: { ...stats, total: Math.max(0, stats.total - excusedTotal) } }
				: {})
		}
	};
	return {
		anomalies: [...anomalies.filter((a) => a.kind !== 'layout_shift'), ...detectLayoutShift(rest)],
		excused,
		problems
	};
}

/** Check a case's expectShift declarations when the journey loads (shape only). */
export function checkExpectShift(caseName, expectShift) {
	if (expectShift === undefined) return;
	if (!Array.isArray(expectShift)) throw new Error(`case ${caseName}: expectShift must be a list`);
	for (const design of expectShift) {
		const ok =
			design &&
			typeof design.step === 'string' &&
			Array.isArray(design.composer) &&
			design.composer.length === 2 &&
			design.composer.every((v) => typeof v === 'string') &&
			design.composer[0] !== design.composer[1] &&
			typeof design.maxValue === 'number' &&
			design.maxValue > 0 &&
			design.maxValue <= 0.5 &&
			typeof design.reason === 'string' &&
			design.reason.length > 0;
		if (!ok) {
			throw new Error(
				`case ${caseName}: expectShift needs { step: <mark label>, composer: [from, to], maxValue: (0, 0.5], reason } (got ${JSON.stringify(design)})`
			);
		}
	}
}

/**
 * The verdict of a case marked `knownBug`, given the plain result.
 *
 * result: { ok, error (stack string | null), anomalies (un-allowed), recorded }
 *   recorded: the recording up to the failure survived (an anomalies array came
 *   back from the recorder, or the case failed after a complete recording, in
 *   `verify`). Without it nothing proves the failure was the known one alone:
 *   it stays a FAIL ("recorder lost").
 *
 * Returns { status: 'pass' | 'fail' | 'known-bug', ok, error, knownBug }.
 */
export function classifyKnownBug(result, known) {
	const base = { status: result.ok ? 'pass' : 'fail', ok: result.ok, error: result.error, knownBug: null };
	if (!known) return base;
	const firstLine = result.error ? result.error.split('\n')[0] : result.anomalies.map((a) => a.message).join(' | ');
	const matchesKnown = !result.ok && known.error.test(firstLine);
	if (matchesKnown && !result.recorded) {
		return {
			...base,
			error: `recorder lost: no flight recording survived this failure, so it cannot be told apart from a flicker before known bug #${known.issue}; it fails as any other failure\n${result.error ?? firstLine}`
		};
	}
	if (matchesKnown && result.anomalies.length === 0) {
		return { status: 'known-bug', ok: true, error: result.error, knownBug: `#${known.issue}: ${known.reason}` };
	}
	if (matchesKnown && result.error) {
		// The known failure, but the recording caught more before it.
		return {
			...base,
			error: `known bug #${known.issue} failed as expected, but the recording also caught: ${result.anomalies.map((a) => a.message).join(' | ')}\n${result.error}`
		};
	}
	if (result.ok && known.intermittent) {
		return { ...base, knownBug: `#${known.issue} did not reproduce this time (intermittent): ${known.reason}` };
	}
	if (result.ok) {
		return {
			status: 'fail',
			ok: false,
			error: `known bug #${known.issue} no longer reproduces here: remove this case's knownBug marker (and close the issue once fixed everywhere)`,
			knownBug: null
		};
	}
	return base;
}
