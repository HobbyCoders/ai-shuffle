/**
 * UsageLimitIndicator — render + interaction behaviour.
 *
 * The pill surfaces ONLY the 5-hour ('five_hour') Claude usage window and opens
 * a popover listing all windows. It renders nothing until the 5-hour window is
 * known — it never falls back to another window. The `rateLimit` store is a
 * singleton; each test resets it to a known state via `applySnapshot` (which
 * replaces the window map).
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

	it('pins the pill to the 5-hour window regardless of higher windows', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [
				{ type: 'five_hour', utilization: 12, resets_at: future() },
				{ type: 'seven_day_overage_included', utilization: 77, resets_at: future() },
			],
		});
		const { container } = render(UsageLimitIndicator);
		// Pill shows the 5-hour window's 12%, NOT the higher 77% Fable window.
		expect(container.querySelector('.usage-text')?.textContent).toBe('12%');
		expect(container.querySelector('.usage-indicator.warning')).toBeNull();
	});

	it('shows the 5-hour pct even when it is lower than the weekly window', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [
				{ type: 'five_hour', utilization: 30, resets_at: future() },
				{ type: 'seven_day', utilization: 85, resets_at: future() },
			],
		});
		const { container } = render(UsageLimitIndicator);
		// Pill tracks five_hour (30%), never the higher weekly (85%) window.
		expect(container.querySelector('.usage-text')?.textContent).toBe('30%');
		expect(container.querySelector('.usage-indicator.warning')).toBeNull();
	});

	it('renders nothing when only non-five_hour windows are known', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'seven_day', utilization: 60, resets_at: future() }],
		});
		const { container } = render(UsageLimitIndicator);
		// No five_hour window → pill hidden (no fallback to another window).
		expect(container.querySelector('.usage-indicator')).toBeNull();
	});

	it('dims the pill (stale) while an account switch is pending', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 42, resets_at: future() }],
		});
		rateLimit.markStale();
		const { container } = render(UsageLimitIndicator);
		// Still shows the last-known number, but visibly dimmed.
		expect(container.querySelector('.usage-text')?.textContent).toBe('42%');
		expect(container.querySelector('.usage-indicator.stale')).not.toBeNull();
	});

	it('clears the stale dimming when the fresh snapshot arrives', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 42, resets_at: future() }],
		});
		rateLimit.markStale();
		// New account's snapshot lands.
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 8, resets_at: future() }],
		});
		const { container } = render(UsageLimitIndicator);
		expect(container.querySelector('.usage-indicator.stale')).toBeNull();
		expect(container.querySelector('.usage-text')?.textContent).toBe('8%');
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
