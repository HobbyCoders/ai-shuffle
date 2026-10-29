// Self-test of how the journey runner judges a case (lib/verdict.mjs).
// No browser, no dev instance:  node --test tests/journeys/lib/verdict.test.mjs
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { checkExpectShift, classifyKnownBug, designedShifts } from './verdict.mjs';
import { composerPositions, expectComposerPositions } from './timeline.mjs';

const KNOWN = { issue: 797, error: /^Error: reply never arrived/, reason: 'the stream skips the restarted log' };
const failure = 'Error: reply never arrived: "You said"\n    at act (cleared-chat.journey.mjs:90:5)';

describe('classifyKnownBug', () => {
	it('reports the known failure as KNOWN BUG when the recording before it is clean', () => {
		const verdict = classifyKnownBug({ ok: false, error: failure, anomalies: [], recorded: true }, KNOWN);
		assert.equal(verdict.status, 'known-bug');
		assert.equal(verdict.ok, true);
		assert.match(verdict.knownBug, /^#797/);
	});

	it('never calls a failure with a LOST recording a known bug (no anomaly list came back)', () => {
		const verdict = classifyKnownBug({ ok: false, error: failure, anomalies: [], recorded: false }, KNOWN);
		assert.equal(verdict.status, 'fail');
		assert.equal(verdict.ok, false);
		assert.match(verdict.error, /^recorder lost/);
		assert.match(verdict.error, /reply never arrived/);
	});

	it('fails the known failure when the recording caught a flicker before it', () => {
		const anomalies = [{ kind: 'reversal', message: 'surface went new → active → new' }];
		const verdict = classifyKnownBug({ ok: false, error: failure, anomalies, recorded: true }, KNOWN);
		assert.equal(verdict.status, 'fail');
		assert.match(verdict.error, /also caught: surface went new/);
	});

	it('fails any other failure, and a pass that should have failed', () => {
		const other = classifyKnownBug({ ok: false, error: 'Error: timeout', anomalies: [], recorded: true }, KNOWN);
		assert.equal(other.status, 'fail');
		assert.equal(other.error, 'Error: timeout');
		const passed = classifyKnownBug({ ok: true, error: null, anomalies: [], recorded: true }, KNOWN);
		assert.equal(passed.status, 'fail');
		assert.match(passed.error, /no longer reproduces/);
		const intermittent = classifyKnownBug({ ok: true, error: null, anomalies: [], recorded: true }, { ...KNOWN, intermittent: true });
		assert.equal(intermittent.status, 'pass');
		assert.match(intermittent.knownBug, /did not reproduce/);
	});

	it('leaves a case without a marker alone', () => {
		assert.equal(classifyKnownBug({ ok: false, error: failure, anomalies: [], recorded: false }, undefined).status, 'fail');
		assert.equal(classifyKnownBug({ ok: true, error: null, anomalies: [], recorded: true }, undefined).status, 'pass');
	});
});

/** A recording: marks, composer states and layout shifts at given times. */
function recording(entries, statsTotal) {
	const timeline = entries.map(([t, kind, data]) =>
		kind === 'mark'
			? { t, kind, label: data }
			: kind === 'state'
				? { t, kind, changes: { composer: data } }
				: { t, kind: 'layout_shift', value: data.value, input: false, sources: data.sources }
	);
	const total = statsTotal ?? timeline.filter((e) => e.kind === 'layout_shift').reduce((s, e) => s + e.value, 0);
	return { timeline, stats: { layoutShift: { total } }, durationMs: 5000 };
}
const SLOT = ['div.input-slot in ChatView'];
const DESIGN = { step: 'open', composer: ['bottom', 'centre'], maxValue: 0.3, reason: 'designed' };
const shiftAnomaly = { kind: 'layout_shift', message: 'layout shifted' };

describe('designedShifts (expectShift)', () => {
	it('excuses exactly the designed move, so a clean case passes', () => {
		const rec = recording([
			[0, 'state', 'bottom'],
			[10, 'mark', 'open'],
			[900, 'state', 'centre'],
			[905, 'shift', { value: 0.12, sources: SLOT }]
		]);
		const judged = designedShifts(rec, [shiftAnomaly], [DESIGN]);
		assert.deepEqual(judged.problems, []);
		assert.deepEqual(judged.anomalies, []);
		assert.equal(judged.excused.length, 1);
		assert.equal(judged.excused[0].value, 0.12);
	});

	it('still flags any other shift in the same step', () => {
		const rec = recording([
			[0, 'state', 'bottom'],
			[10, 'mark', 'open'],
			[900, 'state', 'centre'],
			[905, 'shift', { value: 0.12, sources: SLOT }],
			[2500, 'shift', { value: 0.08, sources: ['div.row in MessageArea'] }]
		]);
		const judged = designedShifts(rec, [shiftAnomaly], [DESIGN]);
		assert.deepEqual(judged.problems, []);
		assert.equal(judged.anomalies.length, 1);
		assert.equal(judged.anomalies[0].kind, 'layout_shift');
		assert.match(judged.anomalies[0].message, /MessageArea/);
	});

	it('does not excuse a shift of something other than the composer layout at the move', () => {
		const rec = recording([
			[0, 'state', 'bottom'],
			[10, 'mark', 'open'],
			[900, 'state', 'centre'],
			[905, 'shift', { value: 0.12, sources: ['div.banner in SidePanel'] }]
		]);
		const judged = designedShifts(rec, [shiftAnomaly], [DESIGN]);
		assert.equal(judged.anomalies.length, 1);
		assert.equal(judged.excused[0].value, 0);
	});

	it('takes the frame the chat layout moved in, whatever else moved with it', () => {
		const rec = recording([
			[0, 'state', 'bottom'],
			[10, 'mark', 'open'],
			[900, 'state', 'centre'],
			[1050, 'shift', { value: 0.09, sources: ['div.empty-hero-content in ChatView', 'div.title-anchor'] }]
		]);
		const judged = designedShifts(rec, [shiftAnomaly], [DESIGN]);
		assert.deepEqual(judged.anomalies, []);
		assert.equal(judged.excused[0].value, 0.09);
	});

	it('does not take a chat-layout shift outside the move\'s window', () => {
		const rec = recording([
			[0, 'state', 'bottom'],
			[10, 'mark', 'open'],
			[900, 'state', 'centre'],
			[2000, 'shift', { value: 0.09, sources: SLOT }]
		]);
		assert.equal(designedShifts(rec, [shiftAnomaly], [DESIGN]).anomalies.length, 1);
	});

	it('fails a larger move than designed', () => {
		const rec = recording([
			[0, 'state', 'bottom'],
			[10, 'mark', 'open'],
			[900, 'state', 'centre'],
			[905, 'shift', { value: 0.45, sources: SLOT }]
		]);
		const judged = designedShifts(rec, [shiftAnomaly], [DESIGN]);
		assert.equal(judged.problems.length, 1);
		assert.match(judged.problems[0], /measured 0\.450, more than its 0\.3/);
		// Not excused, so the shift itself is still reported.
		assert.equal(judged.anomalies.length, 1);
	});

	it('fails an extra move (there and back and there again) and a missing one', () => {
		const twice = recording([
			[0, 'state', 'bottom'],
			[10, 'mark', 'open'],
			[900, 'state', 'centre'],
			[950, 'state', 'bottom'],
			[1000, 'state', 'centre'],
			[1005, 'shift', { value: 0.12, sources: SLOT }]
		]);
		assert.match(designedShifts(twice, [], [DESIGN]).problems[0], /moved bottom → centre 2 times/);
		const never = recording([
			[0, 'state', 'bottom'],
			[10, 'mark', 'open'],
			[11, 'mark', 'next']
		]);
		assert.match(designedShifts(never, [], [DESIGN]).problems[0], /0 times/);
		assert.match(designedShifts(recording([[0, 'state', 'bottom']]), [], [DESIGN]).problems[0], /no step marked "open"/);
	});

	it('counts a move only inside its own step', () => {
		const rec = recording([
			[0, 'state', 'bottom'],
			[10, 'mark', 'before'],
			[500, 'state', 'centre'],
			[505, 'shift', { value: 0.12, sources: SLOT }],
			[600, 'state', 'bottom'],
			[700, 'mark', 'open'],
			[900, 'state', 'centre'],
			[905, 'shift', { value: 0.12, sources: SLOT }]
		]);
		const judged = designedShifts(rec, [shiftAnomaly], [DESIGN]);
		assert.deepEqual(judged.problems, []);
		// The earlier, undeclared move's shift is still there (0.12 >= 0.05).
		assert.equal(judged.anomalies.length, 1);
	});

	it('on a failed run, owes nothing for a step never reached or cut off before the move', () => {
		const cut = recording([
			[0, 'state', 'bottom'],
			[10, 'mark', 'open']
		]);
		assert.deepEqual(designedShifts(cut, [], [DESIGN], { partial: true }).problems, []);
		assert.deepEqual(designedShifts(recording([[0, 'state', 'bottom']]), [], [DESIGN], { partial: true }).problems, []);
	});

	it('refuses a malformed declaration (no blanket allow through expectShift)', () => {
		assert.throws(() => checkExpectShift('c', [{ step: 'open', composer: ['bottom', 'centre'], maxValue: 0.3 }]), /reason/);
		assert.throws(() => checkExpectShift('c', [{ step: 'open', composer: ['bottom'], maxValue: 0.3, reason: 'r' }]), /composer/);
		assert.throws(() => checkExpectShift('c', [{ step: 'open', composer: ['bottom', 'centre'], maxValue: 5, reason: 'r' }]), /maxValue/);
		assert.throws(() => checkExpectShift('c', { step: 'open' }), /list/);
		checkExpectShift('c', [DESIGN]);
		checkExpectShift('c', undefined);
	});
});

describe('composer positions', () => {
	const rec = recording([
		[0, 'state', 'centre'],
		[10, 'mark', 'open'],
		[100, 'state', 'bottom'],
		[116, 'state', 'centre']
	]);
	it('lists every position on any painted frame', () => {
		assert.deepEqual(composerPositions(rec), ['centre', 'bottom', 'centre']);
	});
	it('fails a flicker the designed sequence does not have', () => {
		assert.throws(() => expectComposerPositions(rec, ['centre']), /composer went centre → bottom → centre, designed centre/);
		expectComposerPositions(rec, ['centre', 'bottom', 'centre']);
	});
});
