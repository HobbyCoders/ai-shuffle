/**
 * UsageLimitIndicator — the pill follows the RESOLVED install's bucket.
 *
 * The 5-hour window is per account, and each install runs a different account.
 * The pill must track the bucket of the install that owns the viewed session —
 * an offloaded (remote) session shows the worker's usage, and a remote target
 * with no snapshot yet must show NOTHING (never borrow the local account's
 * numbers). Here we mock `usage-target` to drive the resolved install directly.
 */
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Controllable target store standing in for the real derived one.
let _target: string | null = null;
const _subs = new Set<(v: string | null) => void>();
function setTarget(v: string | null) {
	_target = v;
	for (const fn of _subs) fn(v);
}
vi.mock('$lib/stores/usage-target', () => ({
	usageTargetInstance: {
		get current() {
			return _target;
		},
		subscribe(fn: (v: string | null) => void) {
			_subs.add(fn);
			fn(_target);
			return () => _subs.delete(fn);
		},
	},
}));

import UsageLimitIndicator from './UsageLimitIndicator.svelte';
import { rateLimit } from '$lib/stores/rate-limit';

const future = () => new Date(Date.now() + 3_600_000).toISOString();

function twoInstallSnapshot() {
	rateLimit.applySnapshot({
		available: true,
		windows: [],
		self_instance_id: 'primary',
		snapshots: [
			{
				instance_id: 'primary',
				device_name: 'Studio',
				account_email: 'a@example.com',
				available: true,
				windows: [{ type: 'five_hour', utilization: 12, resets_at: future() }],
			},
			{
				instance_id: 'worker',
				device_name: 'Worker',
				account_email: 'b@example.com',
				available: true,
				windows: [{ type: 'five_hour', utilization: 88, resets_at: future() }],
			},
		],
	});
}

beforeEach(() => {
	setTarget(null);
	// Full store reset — snapshots now MERGE (buckets from a prior test would
	// otherwise leak into the next one).
	rateLimit.__resetForTests();
});
afterEach(() => cleanup());

describe('UsageLimitIndicator — resolved target', () => {
	it('headline follows the resolved bucket (self vs remote)', () => {
		twoInstallSnapshot();

		setTarget('worker');
		const remote = render(UsageLimitIndicator);
		expect(remote.container.querySelector('.usage-text')?.textContent).toBe('88%');
		cleanup();

		setTarget(null); // self → primary
		const local = render(UsageLimitIndicator);
		expect(local.container.querySelector('.usage-text')?.textContent).toBe('12%');
	});

	it('popover shows ONLY the remote install for a remote target', async () => {
		twoInstallSnapshot();
		setTarget('worker');
		const { container } = render(UsageLimitIndicator);
		// Pill already tracks the worker (88%).
		expect(container.querySelector('.usage-text')?.textContent).toBe('88%');

		await fireEvent.click(container.querySelector('.usage-indicator') as HTMLButtonElement);
		// A single section for the resolved remote install — never the self one.
		expect(container.querySelectorAll('.popover-section').length).toBe(1);
		expect(container.querySelectorAll('.popover-row').length).toBe(1);
		expect(container.querySelector('.section-device')?.textContent).toBe('Worker');
		expect(container.textContent).toContain('b@example.com');
		// The self install (Studio / a@example.com / 12%) must NOT appear.
		expect(container.textContent).not.toContain('Studio');
		expect(container.textContent).not.toContain('a@example.com');
		expect(container.textContent).not.toContain('12%');
	});

	it('popover shows ONLY the self install for a local target', async () => {
		twoInstallSnapshot();
		setTarget(null); // self → primary
		const { container } = render(UsageLimitIndicator);
		await fireEvent.click(container.querySelector('.usage-indicator') as HTMLButtonElement);
		expect(container.querySelectorAll('.popover-section').length).toBe(1);
		expect(container.querySelectorAll('.popover-row').length).toBe(1);
		expect(container.querySelector('.section-device')?.textContent).toBe('Studio');
		// The worker install must NOT appear.
		expect(container.textContent).not.toContain('Worker');
		expect(container.textContent).not.toContain('88%');
	});

	it('shows nothing for a remote target with no snapshot yet', () => {
		// Only the self install is known; the viewed session runs on an install we
		// have no bucket for → the pill must NOT fall back to the self numbers.
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [
				{
					instance_id: 'primary',
					device_name: 'Studio',
					available: true,
					windows: [{ type: 'five_hour', utilization: 12, resets_at: future() }],
				},
			],
		});
		setTarget('ghost-worker');
		const { container } = render(UsageLimitIndicator);
		expect(container.querySelector('.usage-indicator')).toBeNull();
	});
});
