/**
 * Tests for the indicator-state derivation (Bug 1 fix).
 *
 * The bottom-of-chat activity indicator is derived purely from
 * (isStreaming, activeTaskCount), with ACTIVE TASKS taking priority:
 *   - tasks > 0              → 'waiting' (calm "Waiting for N task(s)…")
 *   - no tasks + streaming   → 'dots'    (bouncing "...")
 *   - no tasks + parked      → 'none'
 *
 * Active-tasks-first matters because, while parked on a background sub-agent,
 * the backend keeps reporting isStreaming = true for the whole run; checking
 * streaming first would show the "..." dots the entire time (the "stuck" look
 * of Bug 1). The follow-up RESPONSE turn runs with activeTaskCount = 0, so the
 * parent's reply still streams with 'dots'.
 */

import { describe, expect, it } from 'vitest';
import { deriveIndicatorState, waitingLabel } from './indicator-state';

describe('deriveIndicatorState', () => {
	it('prioritizes active tasks: shows waiting even when isStreaming is true (the park)', () => {
		// During the park the SDK loop stays open so isStreaming stays true — the
		// calm waiting indicator must win over the bouncing dots.
		expect(deriveIndicatorState(true, 1)).toBe('waiting');
		expect(deriveIndicatorState(true, 3)).toBe('waiting');
	});

	it('shows the calm waiting indicator when parked with active tasks', () => {
		expect(deriveIndicatorState(false, 1)).toBe('waiting');
		expect(deriveIndicatorState(false, 5)).toBe('waiting');
	});

	it('shows bouncing dots while streaming with no active tasks (normal turn / unpark reply)', () => {
		expect(deriveIndicatorState(true, 0)).toBe('dots');
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
