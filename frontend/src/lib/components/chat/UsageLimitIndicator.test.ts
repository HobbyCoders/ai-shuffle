/**
 * UsageLimitIndicator — render behaviour.
 *
 * Core contract: the pill takes up toolbar space ~90% of the time when empty,
 * so it must render NOTHING until a five-hour reading actually arrives, then
 * stay visible afterwards (including after the window rolls over to 0%).
 *
 * The `rateLimit` store is a singleton whose persisted `lastFiveHour` has no
 * public reset, and it starts `null` (localStorage is empty under jsdom). The
 * "hidden" cases therefore run FIRST, before any five-hour reading is set, and
 * the suite is ordered accordingly (vitest runs in-file tests sequentially).
 */
import { render, cleanup } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';
import UsageLimitIndicator from './UsageLimitIndicator.svelte';
import { rateLimit } from '$lib/stores/rate-limit';

afterEach(() => cleanup());

describe('UsageLimitIndicator', () => {
	it('renders nothing before any five-hour reading arrives', () => {
		const { container } = render(UsageLimitIndicator);
		expect(container.querySelector('.usage-indicator')).toBeNull();
		// Crucially: no stray "—" placeholder taking up space either.
		expect(container.textContent?.trim()).toBe('');
	});

	it('ignores non-five-hour readings (stays hidden)', () => {
		rateLimit.set({
			status: 'allowed_warning',
			rate_limit_type: 'weekly',
			utilization: 0.8,
			resets_at: new Date(Date.now() + 3_600_000).toISOString(),
		});
		const { container } = render(UsageLimitIndicator);
		expect(container.querySelector('.usage-indicator')).toBeNull();
	});

	it('renders the pill once a five-hour reading arrives', () => {
		rateLimit.set({
			status: 'allowed',
			rate_limit_type: 'five_hour',
			utilization: 0.42, // SDK fraction → 42%
			resets_at: new Date(Date.now() + 3_600_000).toISOString(),
		});
		const { container } = render(UsageLimitIndicator);
		expect(container.querySelector('.usage-indicator')).not.toBeNull();
		expect(container.querySelector('.usage-text')?.textContent).toBe('42%');
	});

	it('stays visible at 0% after the window has rolled over', () => {
		rateLimit.set({
			status: 'allowed',
			rate_limit_type: 'five_hour',
			utilization: 0.42,
			resets_at: new Date(Date.now() - 1_000).toISOString(), // already passed
		});
		const { container } = render(UsageLimitIndicator);
		expect(container.querySelector('.usage-indicator')).not.toBeNull();
		expect(container.querySelector('.usage-text')?.textContent).toBe('0%');
	});
});
