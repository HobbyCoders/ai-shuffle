/**
 * UsageLimitIndicator — render + interaction behaviour.
 *
 * The pill surfaces the highest-utilization Claude usage window and opens a
 * popover listing all windows. It renders nothing until at least one window is
 * known. The `rateLimit` store is a singleton; each test resets it to a known
 * state via `applySnapshot` (which replaces the window map).
 */
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import UsageLimitIndicator from './UsageLimitIndicator.svelte';
import { rateLimit } from '$lib/stores/rate-limit';

const future = () => new Date(Date.now() + 3_600_000).toISOString();
const past = () => new Date(Date.now() - 1_000).toISOString();

beforeEach(() => {
	// Reset to an empty-but-available snapshot (clears any prior windows).
	rateLimit.applySnapshot({ windows: [], available: true });
});
afterEach(() => cleanup());

describe('UsageLimitIndicator', () => {
	it('renders nothing when no windows are known', () => {
		const { container } = render(UsageLimitIndicator);
		expect(container.querySelector('.usage-indicator')).toBeNull();
		expect(container.textContent?.trim()).toBe('');
	});

	it('renders the pill with the window percentage (snapshot is 0–100)', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 42, resets_at: future() }],
		});
		const { container } = render(UsageLimitIndicator);
		expect(container.querySelector('.usage-indicator')).not.toBeNull();
		expect(container.querySelector('.usage-text')?.textContent).toBe('42%');
	});

	it('normalises a live SDK fraction (0–1) to a percentage', () => {
		rateLimit.applyLiveEvent({
			status: 'allowed',
			rate_limit_type: 'five_hour',
			utilization: 0.42, // SDK fraction → 42%
			resets_at: future(),
		});
		const { container } = render(UsageLimitIndicator);
		expect(container.querySelector('.usage-text')?.textContent).toBe('42%');
	});

	it('shows 0% once a window has rolled over', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 42, resets_at: past() }],
		});
		const { container } = render(UsageLimitIndicator);
		expect(container.querySelector('.usage-indicator')).not.toBeNull();
		expect(container.querySelector('.usage-text')?.textContent).toBe('0%');
	});

	it('surfaces the highest-utilization window in the pill', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [
				{ type: 'five_hour', utilization: 12, resets_at: future() },
				{ type: 'seven_day_overage_included', utilization: 77, resets_at: future() },
			],
		});
		const { container } = render(UsageLimitIndicator);
		// Headline is the 77% Fable window (also drives the warning severity).
		expect(container.querySelector('.usage-text')?.textContent).toBe('77%');
		expect(container.querySelector('.usage-indicator.warning')).not.toBeNull();
	});

	it('opens a popover listing all windows on click', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [
				{ type: 'five_hour', utilization: 12, resets_at: future() },
				{ type: 'seven_day', utilization: 17, resets_at: future() },
				{ type: 'seven_day_overage_included', utilization: 24, resets_at: future() },
			],
		});
		const { container } = render(UsageLimitIndicator);
		expect(container.querySelector('.usage-popover')).toBeNull();

		const pill = container.querySelector('.usage-indicator') as HTMLButtonElement;
		await fireEvent.click(pill);

		expect(container.querySelector('.usage-popover')).not.toBeNull();
		const rows = container.querySelectorAll('.popover-row');
		expect(rows.length).toBe(3);
		// Labels are humanised per the known-type map.
		expect(container.textContent).toContain('5-hour limit');
		expect(container.textContent).toContain('Weekly (all models)');
		expect(container.textContent).toContain('Fable 5 limit');
	});
});
