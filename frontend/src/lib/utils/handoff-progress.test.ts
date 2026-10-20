import { describe, expect, it } from 'vitest';
import {
	GENERIC_STEP_LABEL,
	HANDOFF_PROGRESS_MAX_EXTRAPOLATE_SECONDS,
	HANDOFF_SLOW_STEP_SECONDS,
	advanceHandoffClock,
	extrapolatedSeconds,
	formatHandoffElapsed,
	handoffStepLabel,
	isSlowHandoffStep,
	progressForEpisode,
	readHandoffClock,
	readHandoffProgress
} from './handoff-progress';

const progress = { step: 'install_history.session_events', elapsed_seconds: 23.4, step_elapsed_seconds: 5.6 };

describe('handoffStepLabel', () => {
	it('maps the receiver steps to short plain labels', () => {
		expect(handoffStepLabel('owner_export', 'BuildPC')).toBe('Packing up the chat');
		expect(handoffStepLabel('decode', 'BuildPC')).toBe('Unpacking the chat');
		expect(handoffStepLabel('install_history', 'BuildPC')).toBe('Copying history');
		expect(handoffStepLabel('git_fetch', 'BuildPC')).toBe('Fetching the branch');
		expect(handoffStepLabel('owner_commit', 'BuildPC')).toBe('Handing the chat over');
		expect(handoffStepLabel('cancel', 'BuildPC')).toBe('Cancelling');
	});

	it('tells history from transcript by the table', () => {
		expect(handoffStepLabel('install_history.session_messages')).toBe('Copying history');
		expect(handoffStepLabel('install_history.session_events')).toBe('Copying history');
		expect(handoffStepLabel('install_history.fluid_session_nodes')).toBe('Copying transcript');
		expect(handoffStepLabel('install_history.fluid_session_events')).toBe('Copying transcript');
		// A table this build does not know still reads as the step it belongs to.
		expect(handoffStepLabel('install_history.some_new_table')).toBe('Copying history');
		expect(handoffStepLabel('publish_history.session_events')).toBe('Saving history');
	});

	it('names the target worker where the step is about it', () => {
		expect(handoffStepLabel('accept_owner', 'BuildPC')).toBe('Starting on BuildPC');
		expect(handoffStepLabel('arm_wakes', 'BuildPC')).toBe('Starting on BuildPC');
		expect(handoffStepLabel('admission', 'BuildPC')).toBe('Getting BuildPC ready');
		expect(handoffStepLabel('accept_owner', '')).toBe('Starting on the other worker');
		expect(handoffStepLabel('accept_owner')).toBe('Starting on the other worker');
	});

	it('falls back to a generic label for unknown, empty or between-step ids', () => {
		expect(handoffStepLabel('quantum_entangle')).toBe(GENERIC_STEP_LABEL);
		expect(handoffStepLabel('next step')).toBe(GENERIC_STEP_LABEL);
		expect(handoffStepLabel('')).toBe(GENERIC_STEP_LABEL);
		expect(handoffStepLabel(null)).toBe(GENERIC_STEP_LABEL);
		expect(handoffStepLabel(undefined)).toBe(GENERIC_STEP_LABEL);
		// Never the raw id, and never an object key from the prototype.
		expect(handoffStepLabel('constructor')).toBe(GENERIC_STEP_LABEL);
		expect(handoffStepLabel('install_history.constructor')).toBe('Copying history');
	});
});

describe('readHandoffProgress', () => {
	it('is null for null, missing or malformed progress', () => {
		expect(readHandoffProgress(null)).toBeNull();
		expect(readHandoffProgress(undefined)).toBeNull();
		expect(readHandoffProgress('install_history')).toBeNull();
		expect(readHandoffProgress({ step: 'decode' })).toBeNull();
		expect(readHandoffProgress({ step: 4, elapsed_seconds: 2 })).toBeNull();
		expect(readHandoffProgress({ step: 'decode', elapsed_seconds: Number.NaN })).toBeNull();
		expect(readHandoffProgress({ step: 'decode', elapsed_seconds: -1 })).toBeNull();
	});

	it('keeps the documented shape, defaulting a missing step time to zero', () => {
		expect(readHandoffProgress(progress)).toEqual(progress);
		expect(readHandoffProgress({ step: 'decode', elapsed_seconds: 3 })).toEqual({ step: 'decode', elapsed_seconds: 3, step_elapsed_seconds: 0 });
	});
});

describe('progressForEpisode', () => {
	const view = (pending: Record<string, unknown> | null) => ({ known: true, pending, seq: 2 });

	it('is null until the receiver reports, and outside sending', () => {
		expect(progressForEpisode({ phase: 'sending', id: 'X' }, null)).toBeNull();
		expect(progressForEpisode({ phase: 'sending', id: 'X' }, { known: false, pending: null })).toBeNull();
		expect(progressForEpisode({ phase: 'sending', id: 'X' }, view(null))).toBeNull();
		expect(progressForEpisode({ phase: 'sending', id: 'X' }, view({ handoff_id: 'X', progress: null }))).toBeNull();
		expect(progressForEpisode({ phase: 'sending', id: 'X' }, view({ handoff_id: 'X' }))).toBeNull();
		for (const phase of ['starting', 'done', 'failed', 'cancelled', 'refused'] as const) {
			expect(progressForEpisode({ phase, id: 'X' }, view({ handoff_id: 'X', progress }))).toBeNull();
		}
	});

	it('reads only the progress of the same move', () => {
		expect(progressForEpisode({ phase: 'sending', id: 'X' }, view({ handoff_id: 'X', progress }))).toEqual(progress);
		expect(progressForEpisode({ phase: 'sending', id: 'X' }, view({ handoff_id: 'Y', progress }))).toBeNull();
		// Older owners omit the id: the one pending move is this one.
		expect(progressForEpisode({ phase: 'sending', id: 'X' }, view({ progress }))).toEqual(progress);
	});
});

describe('timing helpers', () => {
	it('formats elapsed seconds as a clock, never a percentage', () => {
		expect(formatHandoffElapsed(0)).toBe('0:00');
		expect(formatHandoffElapsed(7.9)).toBe('0:07');
		expect(formatHandoffElapsed(125)).toBe('2:05');
		expect(formatHandoffElapsed(3723)).toBe('1:02:03');
		expect(formatHandoffElapsed(-4)).toBe('0:00');
		expect(formatHandoffElapsed(Number.NaN)).toBe('0:00');
	});

	it('calls a step slow only past the threshold', () => {
		expect(isSlowHandoffStep(HANDOFF_SLOW_STEP_SECONDS - 0.1)).toBe(false);
		expect(isSlowHandoffStep(HANDOFF_SLOW_STEP_SECONDS)).toBe(true);
		expect(isSlowHandoffStep(Number.NaN)).toBe(false);
	});

	it('counts on between reads, but not forever', () => {
		expect(extrapolatedSeconds(0)).toBe(0);
		expect(extrapolatedSeconds(-500)).toBe(0);
		expect(extrapolatedSeconds(2500)).toBe(2.5);
		expect(extrapolatedSeconds(10 * 60_000)).toBe(HANDOFF_PROGRESS_MAX_EXTRAPOLATE_SECONDS);
	});
});

describe('advanceHandoffClock', () => {
	const at = (step: string, elapsed: number, stepElapsed: number) => ({ step, elapsed_seconds: elapsed, step_elapsed_seconds: stepElapsed });

	it('is null for null progress', () => {
		expect(advanceHandoffClock(null, null, 1_000)).toBeNull();
		const clock = advanceHandoffClock(null, at('decode', 4, 1), 1_000);
		expect(advanceHandoffClock(clock, null, 2_000)).toBeNull();
	});

	it('keeps counting from when a report was first seen when the same report is read again', () => {
		const first = advanceHandoffClock(null, at('install_history.session_events', 20, 5), 0)!;
		const again = advanceHandoffClock(first, at('install_history.session_events', 20, 5), 3_000)!;
		expect(again).toBe(first);
		expect(readHandoffClock(again, 3_000)).toEqual({ elapsed: 23, stepElapsed: 8 });
	});

	it('never runs the clock backwards on a fresh report', () => {
		const first = advanceHandoffClock(null, at('install_history.session_events', 20, 5), 0)!;
		// Counted to 30 s; the next report says 27 s in the same step.
		const next = advanceHandoffClock(first, at('install_history.session_events', 27, 12), 10_000)!;
		expect(readHandoffClock(next, 10_000)).toEqual({ elapsed: 30, stepElapsed: 15 });
		// A later report runs ahead again and is taken as is.
		const later = advanceHandoffClock(next, at('install_history.session_events', 40, 25), 12_000)!;
		expect(readHandoffClock(later, 12_000)).toEqual({ elapsed: 40, stepElapsed: 25 });
	});

	it('restarts the step time on a new step, and keeps the label and step time between steps', () => {
		const first = advanceHandoffClock(null, at('install_history.session_events', 80, 70), 0)!;
		const between = advanceHandoffClock(first, at('next step', 81, 0), 1_000)!;
		expect(between.step).toBe('install_history.session_events');
		// The previous step's time carries on, so a slow step stays slow.
		expect(readHandoffClock(between, 1_000)).toEqual({ elapsed: 81, stepElapsed: 71 });
		expect(isSlowHandoffStep(readHandoffClock(between, 1_000).stepElapsed)).toBe(true);
		const nextStep = advanceHandoffClock(between, at('install_history.fluid_session_nodes', 82, 1), 2_000)!;
		expect(nextStep.step).toBe('install_history.fluid_session_nodes');
		expect(readHandoffClock(nextStep, 2_000)).toEqual({ elapsed: 82, stepElapsed: 1 });
		// With nothing to keep, a between-steps report shows the generic label.
		expect(advanceHandoffClock(null, at('next step', 3, 0), 0)!.step).toBe('next step');
	});
});
