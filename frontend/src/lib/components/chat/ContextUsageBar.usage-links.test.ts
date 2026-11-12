/**
 * ContextUsageBar — key kinds with no usage meter (`usage_links`): one meta
 * line and the dashboard link the backend names. A metered row renders as
 * before, and a payload from an older worker without the field still renders.
 */
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ContextUsageBar from './ContextUsageBar.svelte';
import {
	rateLimit,
	type ProviderUsageEntry,
	type ProviderUsageLinkEntry,
	type UsageSourceInfo,
} from '$lib/stores/rate-limit';
import { usageSections } from '$lib/stores/expansion-preferences';
import type { PreferenceAdapter } from '$lib/services/preference-sync';

const mocks = vi.hoisted(() => {
	const state = {
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
	return {
		adapters: new Map<string, PreferenceAdapter>(),
		activeSession: {
			get current() {
				return state;
			},
			subscribe(fn: (v: typeof state) => void) {
				fn(state);
				return () => {};
			},
		},
		getContextUsage: vi.fn(),
	};
});

vi.mock('$lib/services/preferences', () => ({
	preferenceSync: {
		register: (key: string, adapter: PreferenceAdapter) => mocks.adapters.set(key, adapter),
		registerPrefix: vi.fn(),
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

const T0 = Date.UTC(2026, 10, 12, 12, 0, 0);
const KIMI_CODE = 'https://www.kimi.com/membership/subscription?tab=quota';
const MINIMAX_BALANCE = 'https://platform.minimax.io/user-center/payment/balance';
const OPENCODE_CONSOLE = 'https://opencode.ai/console';
const KIMI_PLATFORM = 'https://platform.kimi.ai/console/account';

const LINKS: ProviderUsageLinkEntry[] = [
	{ provider_id: 'kimi-code', provider_name: 'Sample Kimi Code', provider_type: 'kimi', kind: 'kimi_code',
		note: 'No quota meter for this key', dashboard_url: KIMI_CODE, subscription: true },
	{ provider_id: 'minimax-api', provider_name: 'Sample MiniMax API', provider_type: 'minimax', kind: 'minimax_api',
		note: 'No balance meter for this key', dashboard_url: MINIMAX_BALANCE, subscription: false },
	{ provider_id: 'opencode-go', provider_name: 'Sample OpenCode Go', provider_type: 'opencode', kind: 'opencode_go',
		note: 'No quota meter for this key', dashboard_url: OPENCODE_CONSOLE, subscription: true },
	{ provider_id: 'opencode-console', provider_name: 'Sample OpenCode Console', provider_type: 'opencode',
		kind: 'opencode_console', note: 'No balance meter for this key', dashboard_url: OPENCODE_CONSOLE, subscription: false },
];

const KIMI_API: ProviderUsageEntry = {
	provider_id: 'kimi-api',
	provider_name: 'Sample Kimi API',
	provider_type: 'kimi_balance',
	source: 'kimi_balance',
	source_info: { id: 'kimi_balance', dashboard_url: KIMI_PLATFORM } as UsageSourceInfo,
	status: 'fresh',
	fetched_at: T0,
	windows: [],
	credits: { balance_remaining: 12.5, currency: 'USD' },
};

async function open() {
	const view = render(ContextUsageBar);
	await fireEvent.click(view.container.querySelector('.usage-indicator') as HTMLButtonElement);
	return view.container;
}

/** Each meta line as "text -> href". */
function metaLines(container: HTMLElement): string[] {
	return Array.from(container.querySelectorAll('.usage-meta')).map((meta) => {
		const text = meta.querySelector('.usage-meta-text')?.textContent ?? '';
		const href = meta.querySelector('a.provider-dashboard-link')?.getAttribute('href') ?? '';
		return `${text} -> ${href}`;
	});
}

beforeEach(() => {
	vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
	vi.setSystemTime(T0);
	vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
	localStorage.clear();
	mocks.adapters.get('context_usage_sections')?.apply(undefined);
	mocks.adapters.get('context_usage_hidden_providers')?.apply(undefined);
	usageSections.set({});
	rateLimit.__resetForTests();
	mocks.getContextUsage.mockReset();
	mocks.getContextUsage.mockResolvedValue({ available: false, reason: 'not_connected' });
});
afterEach(() => {
	cleanup();
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

describe('usage links', () => {
	it('each key kind with no meter gets its line and the vendor page; a metered row is unchanged', async () => {
		rateLimit.applySnapshot({ available: false, windows: [], providers: [KIMI_API], usage_links: LINKS });
		const container = await open();
		const lines = metaLines(container);
		expect(lines).toEqual(expect.arrayContaining([
			`No quota meter for this key -> ${KIMI_CODE}`,
			`No balance meter for this key -> ${MINIMAX_BALANCE}`,
			`No quota meter for this key -> ${OPENCODE_CONSOLE}`,
			`No balance meter for this key -> ${OPENCODE_CONSOLE}`,
			`Checked just now -> ${KIMI_PLATFORM}`,
		]));
		expect(lines.filter((l) => l.startsWith('No '))).toHaveLength(4);
		// The metered row keeps its figure and its own link label.
		expect(container.textContent).toContain('$12.50 left');
		const kimiLink = container.querySelector(`a[href="${KIMI_PLATFORM}"]`);
		expect(kimiLink?.getAttribute('aria-label')).toBe('Compare on the provider dashboard');
		const opencode = container.querySelector(`a[href="${OPENCODE_CONSOLE}"]`) as HTMLAnchorElement;
		expect(opencode.target).toBe('_blank');
		expect(opencode.rel).toContain('noopener');
	});

	it('a payload without usage_links (older worker) renders its rows and no link sections', async () => {
		rateLimit.applySnapshot({ available: false, windows: [], providers: [KIMI_API] });
		const container = await open();
		expect(container.textContent).toContain('$12.50 left');
		expect(metaLines(container).filter((l) => l.startsWith('No '))).toEqual([]);
	});

	it('an older worker\'s provider list clears links; null keeps them', async () => {
		rateLimit.applySnapshot({ available: false, windows: [], providers: [], usage_links: LINKS });
		rateLimit.applySnapshot({ available: false, windows: [], providers: [], usage_links: null });
		let container = await open();
		expect(metaLines(container).filter((l) => l.startsWith('No '))).toHaveLength(4);
		cleanup();
		rateLimit.applySnapshot({ available: false, windows: [], providers: [] });
		container = await open();
		expect(metaLines(container).filter((l) => l.startsWith('No '))).toEqual([]);
	});

	it('a row that has a reading keeps its reading section, not a link', async () => {
		rateLimit.applySnapshot({
			available: false, windows: [], providers: [KIMI_API],
			usage_links: [{ ...LINKS[0], provider_id: 'kimi-api' }],
		});
		const container = await open();
		expect(container.querySelector(`a[href="${KIMI_CODE}"]`)).toBeNull();
		expect(container.textContent).toContain('$12.50 left');
	});

	it('an entry missing its line or URL is skipped', async () => {
		rateLimit.applySnapshot({
			available: false, windows: [], providers: [],
			usage_links: [{ provider_id: 'x', note: 'No quota meter for this key' }, { provider_id: 'y', dashboard_url: KIMI_CODE }],
		});
		const container = await open();
		expect(metaLines(container).filter((l) => l.startsWith('No '))).toEqual([]);
	});
});
