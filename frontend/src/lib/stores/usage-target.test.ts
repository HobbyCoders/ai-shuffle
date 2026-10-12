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
import { resolveTargetDeviceName, resolveTargetInstance } from './usage-target';

type Active = Parameters<typeof resolveTargetInstance>[0];
type Rows = Parameters<typeof resolveTargetInstance>[1];

const active = (over: Partial<Active> = {}): Active => ({
	sessionId: null,
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
		const a = active({ sessionId: 's1' });
		// Viewing an existing local session → self, regardless of stale compose state.
		expect(resolveTargetInstance(a, rows)).toBeNull();
	});

	it('names a network session by its owner device, whatever its instance_id says', () => {
		// On a worker network a mirrored row's instance_id is null and a handed-off
		// one can still name the install it came from; the owner device decides.
		const rows: Rows = [
			{ id: 'mirrored', instance_id: null, owner_device_id: 'dev_unraid' },
			{ id: 'handed-off', instance_id: 'ai-shuffle-unraid', owner_device_id: 'dev_desktopworker' },
			{ id: 'legacy', instance_id: 'worker', owner_device_id: null },
		];
		expect(resolveTargetInstance(active({ sessionId: 'mirrored' }), rows)).toBe('dev_unraid');
		expect(resolveTargetInstance(active({ sessionId: 'handed-off' }), rows)).toBe('dev_desktopworker');
		expect(resolveTargetInstance(active({ sessionId: 'legacy' }), rows)).toBe('worker');
	});

	it('reads the owning device name for the unreported state', () => {
		const rows = [{ id: 's1', device_name: 'ServerWorker' }];
		expect(resolveTargetDeviceName(active({ sessionId: 's1' }), rows)).toBe('ServerWorker');
		expect(resolveTargetDeviceName(active(), rows)).toBeNull();
	});
});
