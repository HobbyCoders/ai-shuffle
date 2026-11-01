/**
 * Settings > Providers usage tabs on a plan without a 5-hour window (Claude
 * Ultra, ChatGPT Pro): a 5-hour window past its reset is left out, a live one
 * is shown, and a tab left open drops the window when it resets (the tab's
 * clock ticks; nothing remounts).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/svelte';
import ClaudeUsage from './ClaudeUsage.svelte';
import SubscriptionUsage from './SubscriptionUsage.svelte';
import type { LoopGuard } from '../loop-guard.svelte';
import type { BankedResets } from '../banked-resets.svelte';
import type { ProviderUsageEntry } from '$lib/stores/rate-limit';

const mocks = vi.hoisted(() => ({ usage: null as { windows: unknown[] } | null }));
vi.mock('$lib/stores/providers.svelte', () => ({
	providers: {
		get current() {
			return { usage: mocks.usage };
		},
	},
}));

const T0 = Date.UTC(2026, 10, 1, 12, 0, 0);
const at = (offsetSec: number) => String(Math.floor(Date.now() / 1000) + offsetSec);
const guard = { status: null } as unknown as LoopGuard;
const banked = { hasLoaded: false, availableCount: 0 } as unknown as BankedResets;

beforeEach(() => {
	vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
	vi.setSystemTime(T0);
});
afterEach(() => {
	cleanup();
	vi.useRealTimers();
	mocks.usage = null;
});

describe('Claude usage tab', () => {
	const windows = (fiveHourResetsAt: string) => [
		{ type: 'five_hour', utilization: 72, resets_at: fiveHourResetsAt },
		{ type: 'seven_day', utilization: 58, resets_at: at(5 * 86_400) },
	];

	it('Ultra: a passed 5-hour window is left out', () => {
		mocks.usage = { windows: windows(at(-60)) };
		const { container } = render(ClaudeUsage, { guard, plan: 'ultra' });
		expect(container.textContent).not.toContain('5-hour limit');
		expect(container.textContent).toContain('Weekly (all models)');
	});

	it('Ultra: a live 5-hour window is shown, then drops out when it resets while the tab is open', async () => {
		mocks.usage = { windows: windows(at(2)) };
		const { container } = render(ClaudeUsage, { guard, plan: 'ultra' });
		expect(container.textContent).toContain('5-hour limit');
		await vi.advanceTimersByTimeAsync(3000);
		expect(container.textContent).not.toContain('5-hour limit');
		expect(container.textContent).toContain('Weekly (all models)');
	});

	it('Max keeps the 5-hour window after it resets', async () => {
		mocks.usage = { windows: windows(at(2)) };
		const { container } = render(ClaudeUsage, { guard, plan: 'max' });
		await vi.advanceTimersByTimeAsync(3000);
		expect(container.textContent).toContain('5-hour limit');
	});

	it('the countdown keeps ticking while the tab is open', async () => {
		mocks.usage = { windows: windows(at(120)) };
		const { container } = render(ClaudeUsage, { guard, plan: 'max' });
		expect(container.textContent).toContain('Resets in 2m 0s');
		await vi.advanceTimersByTimeAsync(30_000);
		expect(container.textContent).toContain('Resets in 1m 30s');
	});
});

describe('ChatGPT usage tab', () => {
	const entry = (plan_type: string, fiveHourResetsAt: string): ProviderUsageEntry => ({
		provider_id: 'codex',
		provider_type: 'chatgpt',
		plan_type,
		status: 'fresh',
		fetched_at: T0,
		windows: [
			{ type: 'five_hour', utilization: 30, resets_at: fiveHourResetsAt },
			{ type: 'seven_day', utilization: 12, resets_at: at(5 * 86_400) },
		],
		credits: null,
	});

	it('Pro: a passed 5-hour window is left out', () => {
		const { container } = render(SubscriptionUsage, { banked, usageEntry: entry('pro', at(-60)), signedIn: false });
		expect(container.textContent).not.toContain('5-hour limit');
	});

	it('Pro: a live 5-hour window drops out when it resets while the tab is open', async () => {
		const { container } = render(SubscriptionUsage, { banked, usageEntry: entry('prolite', at(2)), signedIn: false });
		expect(container.textContent).toContain('5-hour limit');
		expect(container.textContent).toContain('Weekly (all models)');
		await vi.advanceTimersByTimeAsync(3000);
		expect(container.textContent).not.toContain('5-hour limit');
		// The weekly row keeps its label when it becomes the only row.
		expect(container.textContent).toContain('Weekly (all models)');
		expect(container.textContent).not.toContain('Weekly use');
	});

	it('Plus keeps the 5-hour window after it resets', async () => {
		const { container } = render(SubscriptionUsage, { banked, usageEntry: entry('plus', at(2)), signedIn: false });
		await vi.advanceTimersByTimeAsync(3000);
		expect(container.textContent).toContain('5-hour limit');
	});

	it('the account plan stands in when the usage entry has none', () => {
		const usageEntry = { ...entry('pro', at(-60)), plan_type: null };
		const { container } = render(SubscriptionUsage, { banked, usageEntry, signedIn: false, plan: 'pro' });
		expect(container.textContent).not.toContain('5-hour limit');
	});
});
