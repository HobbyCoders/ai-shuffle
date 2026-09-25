/**
 * ContextUsageBar — how each usage state renders: fresh, stale (from the
 * backend, and a persisted "fresh" row that has aged), error, cooldown,
 * `providers: null`, a refresh in progress, and the Claude edge readings.
 * The clock is faked (Date + the popover's one-second tick) so countdowns
 * and ageing are deterministic.
 */
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ContextUsageBar from './ContextUsageBar.svelte';
import { rateLimit, type ProviderUsageEntry } from '$lib/stores/rate-limit';
import { usageSections } from '$lib/stores/expansion-preferences';
import type { PreferenceAdapter } from '$lib/services/preference-sync';

const mocks = vi.hoisted(() => {
	const DEFAULT = {
		sessionId: null as string | null,
		isStreaming: false,
		executionMode: null,
		modelOverride: null as string | null,
		profile: null,
		contextUsed: null as number | null,
		totalTokensIn: 0,
		totalTokensOut: 0,
		totalCacheCreationTokens: 0,
		totalCacheReadTokens: 0,
		liveContextUsage: null as Record<string, unknown> | null,
	};
	type State = typeof DEFAULT;
	let val: State = { ...DEFAULT };
	const subs = new Set<(v: State) => void>();
	return {
		adapters: new Map<string, PreferenceAdapter>(),
		DEFAULT,
		activeSession: {
			get current() {
				return val;
			},
			subscribe(fn: (v: State) => void) {
				fn(val);
				subs.add(fn);
				return () => subs.delete(fn);
			},
			set(v: State) {
				val = v;
				subs.forEach((f) => f(val));
			},
		},
		getContextUsage: vi.fn(),
	};
});

vi.mock('$lib/services/preferences', () => ({
	preferenceSync: {
		register: (key: string, adapter: PreferenceAdapter) => mocks.adapters.set(key, adapter),
		save: vi.fn(),
	},
}));
vi.mock('$lib/stores/active-session', async (importOriginal) => {
	const actual = await importOriginal<Record<string, unknown>>();
	return { ...actual, activeSession: mocks.activeSession };
});
vi.mock('$lib/api/sessions', async (importOriginal) => {
	const actual = await importOriginal<Record<string, unknown>>();
	return { ...actual, getContextUsage: mocks.getContextUsage };
});
vi.mock('$lib/constants/models', async (importOriginal) => {
	const actual = await importOriginal<Record<string, unknown>>();
	return { ...actual, getClaudeAuth: () => ({ auth_kind: 'subscription', plan_type: 'max' }) };
});
vi.mock('$lib/stores/auth.svelte', async (importOriginal) => {
	const actual = await importOriginal<Record<string, unknown>>();
	return {
		...actual,
		claudeAuthenticated: {
			subscribe(fn: (value: boolean) => void) {
				fn(true);
				return () => {};
			},
		},
	};
});

const T0 = Date.UTC(2026, 8, 25, 12, 0, 0);

function row(overrides: Partial<ProviderUsageEntry> & { provider_id: string }): ProviderUsageEntry {
	return {
		provider_name: overrides.provider_id,
		provider_type: 'openrouter',
		status: 'fresh',
		fetched_at: T0,
		windows: [],
		credits: null,
		...overrides,
	};
}

function snapshot(providers: ProviderUsageEntry[] | null, extra: Record<string, unknown> = {}) {
	rateLimit.applySnapshot({ available: false, windows: [], providers, ...extra });
}

async function open() {
	const view = render(ContextUsageBar);
	await fireEvent.click(view.container.querySelector('.usage-indicator') as HTMLButtonElement);
	return view.container;
}

function sectionText(container: HTMLElement, name: string): string {
	// A provider's rows follow its header section until the next section.
	const sections = Array.from(container.querySelectorAll('.popover-section'));
	const index = sections.findIndex((s) => s.querySelector('.section-device')?.textContent === name);
	if (index < 0) return '';
	let text = sections[index].textContent ?? '';
	for (let el = sections[index].nextElementSibling; el && !el.classList.contains('popover-section'); el = el.nextElementSibling) {
		text += ' ' + (el.textContent ?? '');
	}
	return text;
}

beforeEach(() => {
	vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
	vi.setSystemTime(T0);
	// Reduced motion: usage numbers render without the flip animation.
	vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
	localStorage.clear();
	mocks.adapters.get('context_breakdown_open')?.apply(undefined);
	mocks.adapters.get('context_usage_sections')?.apply(undefined);
	mocks.adapters.get('context_usage_hidden_providers')?.apply(undefined);
	usageSections.set({});
	rateLimit.__resetForTests();
	mocks.activeSession.set({ ...mocks.DEFAULT });
	mocks.getContextUsage.mockReset();
	mocks.getContextUsage.mockResolvedValue({ available: false, reason: 'not_connected' });
});
afterEach(() => {
	cleanup();
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

describe('provider rows', () => {
	it('fresh: figures without an "Updated" line', async () => {
		snapshot([row({ provider_id: 'Router', windows: [{ type: 'credit_limit', utilization: 20, resets_at: null }] })]);
		const text = sectionText(await open(), 'Router');
		expect(text).toContain('20%');
		expect(text).not.toContain('Updated');
	});

	it('stale from the backend: age and next check, which clears once the cooldown passes', async () => {
		snapshot([row({
			provider_id: 'Router', status: 'stale', fetched_at: T0 - 12 * 60_000, cooldown_until: T0 + 90_000,
			credits: { usage: 1.5 },
		})]);
		const container = await open();
		expect(sectionText(container, 'Router')).toContain('Updated 12m ago · next check in 1m 30s');
		await vi.advanceTimersByTimeAsync(91_000);
		const text = sectionText(container, 'Router');
		expect(text).toContain('Updated 13m ago');
		expect(text).not.toContain('next check');
	});

	it('a persisted "fresh" row that has aged reads as stale', async () => {
		snapshot([row({ provider_id: 'Router', status: 'fresh', fetched_at: T0, credits: { usage: 1.5 } })]);
		vi.setSystemTime(T0 + 2 * 3_600_000);
		expect(sectionText(await open(), 'Router')).toContain('Updated 2h ago');
	});

	it('error: one provider’s failure is labelled and the others still render', async () => {
		snapshot([
			row({ provider_id: 'Broken', provider_type: 'grok', status: 'unavailable', fetched_at: null, last_error_kind: 'billing_access_denied' }),
			row({ provider_id: 'Router', credits: { usage: 2 } }),
		]);
		const container = await open();
		// Grok rows carry the brand name.
		expect(sectionText(container, 'Grok')).toContain('Billing access required');
		expect(sectionText(container, 'Router')).toContain('$2.00 used');
	});

	it('providers: null keeps the rows already shown', async () => {
		snapshot([row({ provider_id: 'Router', credits: { usage: 2 } })]);
		snapshot(null);
		expect(sectionText(await open(), 'Router')).toContain('$2.00 used');
	});

	it('refreshing: progressive rows render while the pass is still running', async () => {
		snapshot([row({ provider_id: 'Router', credits: { usage: 2 } })], { provider_refreshing: true });
		const container = await open();
		snapshot([row({ provider_id: 'Router', credits: { usage: 3 } })], { provider_refreshing: true });
		await vi.advanceTimersByTimeAsync(0);
		expect(sectionText(container, 'Router')).toContain('$3.00 used');
	});

	it('subscription quota at the edges: full use is 0% left, a sliver used is 99% left', async () => {
		snapshot([row({
			provider_id: 'ChatGPT', provider_type: 'chatgpt',
			windows: [
				{ type: 'five_hour', utilization: 100, resets_at: String(Math.floor(T0 / 1000) + 3600) },
				{ type: 'seven_day', utilization: 0.3, resets_at: String(Math.floor(T0 / 1000) + 86_400) },
			],
		})]);
		// ChatGPT subscription rows carry the OpenAI brand.
		const text = sectionText(await open(), 'OpenAI');
		expect(text).toContain('0% left');
		expect(text).toContain('99% left');
		expect(text).toContain('resets in 1h 0m');
	});
});

describe('Claude rows', () => {
	it('a nearly exhausted window reads 1% left, and a passed reset reads as awaiting the next report', async () => {
		rateLimit.applySnapshot({
			available: true,
			fetched_at: T0 / 1000,
			windows: [
				{ type: 'five_hour', utilization: 0.4, resets_at: '2026-09-25T13:00:00' },
				{ type: 'seven_day', utilization: 30, resets_at: String(Math.floor(T0 / 1000) - 60) },
			],
		});
		const container = await open();
		const text = container.textContent ?? '';
		expect(text).toContain('1% left');
		// Zone-less reset is UTC: one hour from T0 in every browser zone.
		expect(text).toContain('resets in 1h 0m');
		expect(text).toContain('reset · awaiting next report');
		expect(text).toContain('100% left');
		expect(text).not.toContain('resets in -');
	});

	it('a live event from the session’s own turn updates the open panel', async () => {
		rateLimit.applySnapshot({ available: true, fetched_at: T0 / 1000, windows: [
			{ type: 'five_hour', utilization: 80, resets_at: String(Math.floor(T0 / 1000) + 3600) },
		] });
		const container = await open();
		expect(container.textContent).toContain('80% left');
		rateLimit.applyLiveEvent({ rate_limit_type: 'five_hour', utilization: 0.35, resets_at: String(Math.floor(T0 / 1000) + 3600) });
		await vi.advanceTimersByTimeAsync(0);
		expect(container.textContent).toContain('65% left');
	});

	it('shows how old the oldest window is once it passes five minutes', async () => {
		rateLimit.applySnapshot({ available: true, fetched_at: T0 / 1000, windows: [
			{ type: 'five_hour', utilization: 80, resets_at: null, observed_at: T0 },
			{ type: 'seven_day', utilization: 50, resets_at: null, observed_at: T0 - 20 * 60_000 },
		] });
		const container = await open();
		expect(container.querySelector('.usage-as-of')?.textContent).toBe('Updated 20m ago');
	});
});
