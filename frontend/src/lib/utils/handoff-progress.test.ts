import { describe, expect, it } from 'vitest';
import {
	GENERIC_STEP_LABEL,
	HANDOFF_PROGRESS_MAX_EXTRAPOLATE_SECONDS,
	HANDOFF_SLOW_STEP_SECONDS,
	extrapolatedSeconds,
	formatHandoffElapsed,
	handoffStepLabel,
	isSlowHandoffStep,
	progressForEpisode,
	readHandoffProgress
} from './handoff-progress';

const progress = { step: 'install_history.session_events', elapsed_seconds: 23.4, step_elapsed_seconds: 5.6 };

describe('handoffStepLabel', () => {
	it('maps the receiver steps to short plain labels', () => {
		expect(handoffStepLabel('owner_export', 'GamingPC')).toBe('Packing up the chat');
		expect(handoffStepLabel('decode', 'GamingPC')).toBe('Unpacking the chat');
		expect(handoffStepLabel('install_history', 'GamingPC')).toBe('Copying history');
		expect(handoffStepLabel('git_fetch', 'GamingPC')).toBe('Fetching the branch');
		expect(handoffStepLabel('owner_commit', 'GamingPC')).toBe('Handing the chat over');
		expect(handoffStepLabel('cancel', 'GamingPC')).toBe('Cancelling');
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
		expect(handoffStepLabel('accept_owner', 'GamingPC')).toBe('Starting on GamingPC');
		expect(handoffStepLabel('arm_wakes', 'GamingPC')).toBe('Starting on GamingPC');
		expect(handoffStepLabel('admission', 'GamingPC')).toBe('Getting GamingPC ready');
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
