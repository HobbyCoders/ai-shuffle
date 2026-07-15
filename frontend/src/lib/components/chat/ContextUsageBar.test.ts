/**
 * ContextUsageBar — render + interaction behaviour for the MERGED pill.
 *
 * One glass pill (context-window ring + percent) opens ONE popover with two
 * sections: the context-window breakdown (collapsible) and the resolved
 * install's plan usage limits. The pill is ALWAYS visible; the limits section
 * shows the resolved bucket's windows, or "No usage data yet" when empty.
 *
 * The `rateLimit` store is a singleton; each test resets it via
 * `__resetForTests` and clears localStorage so the persisted breakdown-open
 * flag starts collapsed.
 */
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import ContextUsageBar from './ContextUsageBar.svelte';
import { rateLimit } from '$lib/stores/rate-limit';

const future = () => new Date(Date.now() + 3_600_000).toISOString();
const past = () => new Date(Date.now() - 1_000).toISOString();

function openPopover(container: HTMLElement) {
	const pill = container.querySelector('.usage-indicator') as HTMLButtonElement;
	return fireEvent.click(pill);
}

beforeEach(() => {
	localStorage.clear();
	// Full reset: clears every bucket AND the per-install generation guard.
	rateLimit.__resetForTests();
});
afterEach(() => cleanup());

describe('ContextUsageBar — pill', () => {
	it('always renders the pill with a progress ring and percent text', () => {
		const { container } = render(ContextUsageBar);
		const pill = container.querySelector('.usage-indicator');
		expect(pill).not.toBeNull();
		const ring = container.querySelector('svg.usage-ring');
		expect(ring).not.toBeNull();
		// Background track + progress circle.
		expect(ring?.querySelectorAll('circle').length).toBe(2);
		expect(container.querySelector('.usage-text')?.textContent).toMatch(/^\d+%$/);
	});

	it('renders the pill even when no usage windows are known', () => {
		const { container } = render(ContextUsageBar);
		expect(container.querySelector('.usage-indicator')).not.toBeNull();
	});
});

describe('ContextUsageBar — context breakdown chevron', () => {
	it('toggles the category breakdown and persists the state to localStorage', async () => {
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		// Collapsed by default — no breakdown rows rendered.
		expect(container.querySelectorAll('.breakdown-row').length).toBe(0);
		expect(localStorage.getItem('aihub.contextBreakdownOpen')).not.toBe('1');

		const chevron = container.querySelector('.ctx-chevron') as HTMLButtonElement;
		await fireEvent.click(chevron);
		// Expanded — the estimate breakdown rows appear and the flag persists.
		expect(container.querySelectorAll('.breakdown-row').length).toBeGreaterThan(0);
		expect(localStorage.getItem('aihub.contextBreakdownOpen')).toBe('1');

		await fireEvent.click(chevron);
		expect(container.querySelectorAll('.breakdown-row').length).toBe(0);
		expect(localStorage.getItem('aihub.contextBreakdownOpen')).toBe('0');
	});
});

describe('ContextUsageBar — plan usage limits', () => {
	it('shows "No usage data yet" when no windows are known', async () => {
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.usage-popover')).not.toBeNull();
		expect(container.textContent).toContain('No usage data yet');
		expect(container.querySelectorAll('.popover-row').length).toBe(0);
	});

	it('lists the resolved window percentage (snapshot is 0–100)', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 42, resets_at: future() }],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		const rows = container.querySelectorAll('.popover-row');
		expect(rows.length).toBe(1);
		expect(rows[0].querySelector('.row-pct')?.textContent).toBe('42%');
	});

	it('normalises a live SDK fraction (0–1) to a percentage', async () => {
		rateLimit.applyLiveEvent({
			status: 'allowed',
			rate_limit_type: 'five_hour',
			utilization: 0.42, // SDK fraction → 42%
			resets_at: future(),
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.popover-row .row-pct')?.textContent).toBe('42%');
	});

	it('shows 0% once a window has rolled over', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 42, resets_at: past() }],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.popover-row .row-pct')?.textContent).toBe('0%');
	});

	it('lists all windows with humanised labels on open', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [
				{ type: 'five_hour', utilization: 12, resets_at: future() },
				{ type: 'seven_day', utilization: 17, resets_at: future() },
				{ type: 'seven_day_overage_included', utilization: 24, resets_at: future() },
			],
		});
		const { container } = render(ContextUsageBar);
		expect(container.querySelector('.usage-popover')).toBeNull();

		await openPopover(container);

		expect(container.querySelector('.usage-popover')).not.toBeNull();
		const rows = container.querySelectorAll('.popover-row');
		expect(rows.length).toBe(3);
		// A lone unlabeled install keeps the flat limits list (no section header).
		expect(container.querySelector('.popover-section')).toBeNull();
		expect(container.textContent).toContain('5-hour limit');
		expect(container.textContent).toContain('Weekly (all models)');
		expect(container.textContent).toContain('Fable 5 limit');
	});

	it('shows ONLY the resolved (self) install — never other installs windows', async () => {
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
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		// A single section + row for the resolved (self) install only.
		expect(container.querySelectorAll('.popover-section').length).toBe(1);
		expect(container.querySelectorAll('.popover-row').length).toBe(1);
		expect(container.textContent).toContain('Studio');
		expect(container.textContent).toContain('a@example.com');
		expect(container.textContent).not.toContain('Worker');
		expect(container.textContent).not.toContain('b@example.com');
		expect(container.textContent).not.toContain('88%');
	});

	it('heads the self section with the resolved install device + account label', async () => {
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
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelectorAll('.popover-section').length).toBe(1);
		expect(container.querySelector('.section-device')?.textContent).toBe('Studio');
		expect(container.querySelector('.section-account')?.textContent).toBe('a@example.com');
	});

	it('unlabeled self keeps a flat limits list (no header, no other installs)', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [
				{
					instance_id: 'primary',
					available: true,
					windows: [{ type: 'five_hour', utilization: 12, resets_at: future() }],
				},
			],
		});
		rateLimit.applyLiveEvent({
			rate_limit_type: 'five_hour',
			utilization: 0.5,
			resets_at: future(),
			instance_id: 'worker',
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		// Unlabeled self → no section header at all.
		expect(container.querySelector('.popover-section')).toBeNull();
		expect(container.textContent).not.toContain('This device');
		expect(container.textContent).not.toContain('Other device');
		// Only the self window is shown (12%), not the worker's live 50%.
		expect(container.querySelectorAll('.popover-row').length).toBe(1);
		expect(container.textContent).not.toContain('50%');
	});
});
