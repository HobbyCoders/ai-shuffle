/**
 * usage-target — session → owning-install resolution.
 *
 * Exercises the pure `resolveTargetInstance` resolver that decides which
 * install's usage bucket the widget should show:
 *   1. an active session → the row's `instance_id` (null = local/self);
 *   2. a new chat → self (null);
 *   3. row not found → self (null).
 */
import { describe, expect, it } from 'vitest';
import { resolveTargetInstance } from './usage-target';

type Active = Parameters<typeof resolveTargetInstance>[0];
type Rows = Parameters<typeof resolveTargetInstance>[1];

const active = (over: Partial<Active> = {}): Active => ({
	sessionId: null,
	executionMode: 'local',
	cloudTargetInstanceId: null,
	...over,
});

describe('resolveTargetInstance', () => {
	it('returns the active session row instance_id (remote session)', () => {
		const rows: Rows = [{ id: 's1', instance_id: 'worker' }];
		expect(resolveTargetInstance(active({ sessionId: 's1' }), rows)).toBe('worker');
	});

	it('returns null for a local session (row has no instance_id)', () => {
		const rows: Rows = [{ id: 's1', instance_id: null }];
		expect(resolveTargetInstance(active({ sessionId: 's1' }), rows)).toBeNull();
	});

	it('falls back to self when the row is not in the list', () => {
		expect(resolveTargetInstance(active({ sessionId: 'missing' }), [])).toBeNull();
	});

	it('is self for a new local chat', () => {
		expect(resolveTargetInstance(active(), [])).toBeNull();
	});

	it('an active session takes precedence over stale compose fields', () => {
		const rows: Rows = [{ id: 's1', instance_id: null }];
		const a = active({ sessionId: 's1', cloudTargetInstanceId: 'worker' });
		// Viewing an existing local session → self, regardless of stale compose state.
		expect(resolveTargetInstance(a, rows)).toBeNull();
	});
});
