/**
 * Tests for the indicator-state derivation (Bug 1 fix).
 *
 * The bottom-of-chat activity indicator is derived purely from
 * (isStreaming, activeTaskCount):
 *   - streaming                       → 'dots'    (bouncing "...")
 *   - parked (!streaming) + tasks > 0 → 'waiting' (calm "Waiting for N task(s)…")
 *   - parked + no tasks               → 'none'
 *
 * This guarantees the "..." dots never persist after a turn parks waiting on
 * background work.
 */

import { describe, expect, it } from 'vitest';
import { deriveIndicatorState, waitingLabel } from './indicator-state';

describe('deriveIndicatorState', () => {
	it('shows bouncing dots while streaming (regardless of task count)', () => {
		expect(deriveIndicatorState(true, 0)).toBe('dots');
		expect(deriveIndicatorState(true, 3)).toBe('dots');
	});

	it('shows the calm waiting indicator when parked with active tasks', () => {
		expect(deriveIndicatorState(false, 1)).toBe('waiting');
		expect(deriveIndicatorState(false, 5)).toBe('waiting');
	});

	it('shows nothing when parked with no active tasks', () => {
		expect(deriveIndicatorState(false, 0)).toBe('none');
	});
});

describe('waitingLabel', () => {
	it('uses the singular noun for exactly one task', () => {
		expect(waitingLabel(1)).toBe('Waiting for 1 background task…');
	});

	it('uses the plural noun for multiple tasks', () => {
		expect(waitingLabel(2)).toBe('Waiting for 2 background tasks…');
		expect(waitingLabel(7)).toBe('Waiting for 7 background tasks…');
	});
});
