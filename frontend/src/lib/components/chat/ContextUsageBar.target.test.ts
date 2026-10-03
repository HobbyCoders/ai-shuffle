/**
 * ContextUsageBar — the plan-usage-limits section follows the RESOLVED install.
 *
 * The 5-hour window is per account, and each install runs a different account.
 * The limits section must track the bucket of the install that owns the viewed
 * session — an offloaded (remote) session shows the worker's usage, and a remote
 * target with no account snapshot hides Claude (never showing the local
 * account's numbers). Here we mock `usage-target` to drive the resolved install
 * directly.
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
let _deviceName: string | null = null;
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
	usageTargetDeviceName: {
		get current() {
			return _deviceName;
		},
		subscribe(fn: (v: string | null) => void) {
			fn(_deviceName);
			return () => {};
		},
	},
}));

import ContextUsageBar from './ContextUsageBar.svelte';
import { rateLimit } from '$lib/stores/rate-limit';

const future = () => new Date(Date.now() + 3_600_000).toISOString();

function openPopover(container: HTMLElement) {
	const pill = container.querySelector('.usage-indicator') as HTMLButtonElement;
	return fireEvent.click(pill);
}

function twoInstallSnapshot() {
	rateLimit.applySnapshot({
		available: true,
		windows: [],
		self_instance_id: 'primary',
		snapshots: [
			{
				instance_id: 'primary',
				device_name: 'Studio',
				available: true,
				windows: [{ type: 'five_hour', utilization: 12, resets_at: future(), observed_at: Date.now() }],
			},
			{
				instance_id: 'worker',
				device_name: 'Worker',
				available: true,
				windows: [{ type: 'five_hour', utilization: 88, resets_at: future(), observed_at: Date.now() }],
			},
		],
	});
}

beforeEach(() => {
	setTarget(null);
	_deviceName = null;
	localStorage.clear();
	rateLimit.__resetForTests();
});
afterEach(() => cleanup());

describe('ContextUsageBar — resolved target', () => {
	it('limits section follows the resolved bucket (self vs remote)', async () => {
		twoInstallSnapshot();

		setTarget('worker');
		const remote = render(ContextUsageBar);
		await openPopover(remote.container);
		expect(remote.container.querySelector('.popover-row .row-pct')?.textContent).toBe('88% left');
		cleanup();

		setTarget(null); // self → primary
		const local = render(ContextUsageBar);
		await openPopover(local.container);
		expect(local.container.querySelector('.popover-row .row-pct')?.textContent).toBe('12% left');
	});

	it('popover shows ONLY the remote install for a remote target', async () => {
		twoInstallSnapshot();
		setTarget('worker');
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		// A single section + row for the resolved remote install — never the self one.
		expect(container.querySelectorAll('.popover-section').length).toBe(1);
		expect(container.querySelectorAll('.popover-row').length).toBe(1);
		// Header is the fixed "Claude" label + the resolved install's device name;
		// the account email never appears (ChatGPT-provider parity).
		expect(container.querySelector('.section-device')?.textContent).toBe('Claude');
		expect(container.querySelector('.section-account')?.textContent).toBe('Worker');
		expect(container.textContent).not.toContain('b@example.com');
		expect(container.querySelector('.popover-row .row-pct')?.textContent).toBe('88% left');
		// The self install (Studio / a@example.com / 12% left) must NOT appear.
		expect(container.textContent).not.toContain('Studio');
		expect(container.textContent).not.toContain('a@example.com');
		expect(container.textContent).not.toContain('12%');
	});

	it('popover shows ONLY the self install for a local target', async () => {
		twoInstallSnapshot();
		setTarget(null); // self → primary
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelectorAll('.popover-section').length).toBe(1);
		expect(container.querySelectorAll('.popover-row').length).toBe(1);
		// Header is the fixed "Claude" label + the resolved self install's device name.
		expect(container.querySelector('.section-device')?.textContent).toBe('Claude');
		expect(container.querySelector('.section-account')?.textContent).toBe('Studio');
		expect(container.querySelector('.popover-row .row-pct')?.textContent).toBe('12% left');
		// The worker install must NOT appear.
		expect(container.textContent).not.toContain('Worker');
		expect(container.textContent).not.toContain('88%');
	});

	it('keeps Claude visible, unreported, for a remote target with no account snapshot', async () => {
		// Only the self install is known; the viewed session runs on an install we
		// have no bucket for → the limits section must NOT fall back to self numbers,
		// and must not vanish either: it says that device has not reported.
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
		_deviceName = 'Unraid';
		const { container } = render(ContextUsageBar);
		// The pill is always present now.
		expect(container.querySelector('.usage-indicator')).not.toBeNull();
		await openPopover(container);
		expect(container.querySelector('.section-device')?.textContent).toBe('Claude');
		expect(container.querySelector('.usage-as-of')?.textContent).toBe('Not reported from Unraid yet');
		expect(container.textContent).toContain('Unraid has not shared its Claude usage with this');
		expect(container.querySelectorAll('.popover-row').length).toBe(0);
		// Never borrow the self install's window/label.
		expect(container.textContent).not.toContain('Studio');
		expect(container.textContent).not.toContain('12%');
		expect(container.textContent).not.toContain('88%');
	});

	it('hides the Claude section for a remote install whose CLI has reported no usage', async () => {
		// Snapshots carry no account identity, so an empty remote bucket is not
		// proof that the worker is signed in to Claude.
		rateLimit.applySnapshot({ available: false, windows: [], self_instance_id: 'primary', snapshots: [
			{ instance_id: 'worker', available: false, windows: [] },
		] });
		setTarget('worker');
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.section-device')).toBeNull();
		expect(container.textContent).not.toContain('No usage data yet');
	});

	it('shows a remote install once its CLI has reported a window', async () => {
		rateLimit.applySnapshot({ available: false, windows: [], self_instance_id: 'primary', snapshots: [
			{ instance_id: 'worker', available: true, windows: [{ type: 'five_hour', utilization: 40, resets_at: null }] },
		] });
		setTarget('worker');
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.section-device')?.textContent).toBe('Claude');
		expect(container.querySelector('.popover-row .row-pct')?.textContent).toBe('40% left');
	});
});
