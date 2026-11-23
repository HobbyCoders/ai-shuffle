/**
 * ContextUsageBar — the Subscriptions / API keys groups. Each group starts
 * collapsed and remembers its open state. Collapsed, a group shows only what
 * the current chat uses: the plan it runs on (its weekly window), or the API
 * key it runs on, else the paid API its tools last billed. The account the
 * chat uses is tagged "in use" inside an open group.
 */
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ContextUsageBar from './ContextUsageBar.svelte';
import { rateLimit, type ProviderUsageEntry, type ProviderUsageLinkEntry, type UsageSourceInfo } from '$lib/stores/rate-limit';
import { usageSections } from '$lib/stores/expansion-preferences';
import type { PreferenceAdapter } from '$lib/services/preference-sync';
import type { ChatMessage } from '$lib/stores/types';

const mocks = vi.hoisted(() => {
	const state = {
		sessionId: 'sample-chat' as string | null,
		isStreaming: false,
		executionMode: null,
		modelOverride: null as string | null,
		liveProvider: null as string | null,
		messages: [] as unknown[],
		profile: null,
		contextUsed: null as number | null,
		totalTokensIn: 0,
		totalTokensOut: 0,
		totalCacheCreationTokens: 0,
		totalCacheReadTokens: 0,
		liveContextUsage: null as Record<string, unknown> | null,
	};
	return {
		state,
		claudeAuth: { auth_kind: 'subscription' as 'subscription' | 'api' | null, plan_type: null as string | null },
		/** The install owning the chat; null is this one. */
		target: null as string | null,
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
vi.mock('$lib/constants/models', async (importOriginal) => {
	const actual = await importOriginal<Record<string, unknown>>();
	return { ...actual, getClaudeAuth: () => mocks.claudeAuth };
});
vi.mock('$lib/stores/usage-target', () => {
	const store = (get: () => string | null) => ({
		get current() {
			return get();
		},
		subscribe(fn: (v: string | null) => void) {
			fn(get());
			return () => {};
		},
	});
	return { usageTargetInstance: store(() => mocks.target), usageTargetDeviceName: store(() => null) };
});
vi.mock('$lib/api/sessions', async (importOriginal) => {
	const actual = await importOriginal<Record<string, unknown>>();
	return { ...actual, getContextUsage: mocks.getContextUsage };
});

const T0 = Date.UTC(2026, 10, 23, 12, 0, 0);
const inHour = () => new Date(T0 + 3_600_000).toISOString();
const OPENAI_USAGE = 'https://platform.openai.com/usage';

const FAL: ProviderUsageEntry = {
	provider_id: 'fal-ai',
	provider_name: 'fal.ai',
	provider_type: 'fal',
	source: 'fal_balance',
	source_info: { id: 'fal_balance', dashboard_url: 'https://fal.ai/dashboard/billing' } as UsageSourceInfo,
	status: 'fresh',
	fetched_at: T0,
	windows: [],
	credits: { balance_remaining: 8.4, currency: 'USD' },
};
const KIMI_API: ProviderUsageEntry = {
	...FAL,
	provider_id: 'kimi-api',
	provider_name: 'Sample Kimi API',
	provider_type: 'kimi_balance',
	source: 'kimi_balance',
	source_info: { id: 'kimi_balance', dashboard_url: 'https://platform.kimi.ai/console/account' } as UsageSourceInfo,
	credits: { balance_remaining: 12.5, currency: 'USD' },
};
const OPENAI_LINK: ProviderUsageLinkEntry = {
	provider_id: 'sample-openai', provider_name: 'Sample OpenAI', provider_type: 'openai', kind: 'openai_api',
	note: 'Credit balance is shown only on the OpenAI Platform', dashboard_url: OPENAI_USAGE, subscription: false,
};

function snapshot() {
	rateLimit.applySnapshot({
		available: true,
		fetched_at: T0 / 1000,
		windows: [
			{ type: 'five_hour', utilization: 70, resets_at: inHour(), observed_at: T0 },
			{ type: 'seven_day', utilization: 40, resets_at: inHour(), observed_at: T0 },
		],
		providers: [FAL, KIMI_API],
		usage_links: [OPENAI_LINK],
	});
}

function falImage(): ChatMessage {
	return {
		id: 'tool-1', role: 'assistant', type: 'tool_use', content: '',
		toolName: 'mcp__ai-tools__generateImage', toolStatus: 'complete',
		toolResult: JSON.stringify({ status: 'completed', choice: { source: 'fal', account: 'fal-ai' }, result: { success: true } }),
	} as ChatMessage;
}

async function open() {
	const view = render(ContextUsageBar);
	await fireEvent.click(view.container.querySelector('.usage-indicator') as HTMLButtonElement);
	return view.container;
}

function group(container: HTMLElement, id: string) {
	const el = container.querySelector(`.usage-group[data-group="${id}"]`) as HTMLElement;
	const header = el?.querySelector('.group-header') as HTMLButtonElement;
	return {
		el,
		header,
		summary: () => el.querySelector('.group-summary')?.textContent?.replace(/\s+/g, ' ').trim() ?? '',
		sections: () => Array.from(el.querySelectorAll('.section-device')).map((s) => s.textContent),
	};
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
	Object.assign(mocks.state, { modelOverride: null, liveProvider: null, messages: [] });
	mocks.claudeAuth = { auth_kind: 'subscription', plan_type: null };
	mocks.target = null;
	mocks.getContextUsage.mockReset();
	mocks.getContextUsage.mockResolvedValue({ available: false, reason: 'not_connected' });
});
afterEach(() => {
	cleanup();
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

describe('usage groups', () => {
	it('start collapsed: a Claude-plan chat shows its weekly window and the API group stays empty', async () => {
		snapshot();
		const container = await open();
		const subs = group(container, 'group:subscriptions');
		const apis = group(container, 'group:api');
		expect(subs.header.getAttribute('aria-expanded')).toBe('false');
		expect(apis.header.getAttribute('aria-expanded')).toBe('false');
		expect(subs.sections()).toEqual([]);
		expect(subs.summary()).toMatch(/^Claude · Weekly \d+% left$/);
		expect(apis.summary()).toBe('');
		// Counts name every account in the group, used or not.
		expect(apis.header.querySelector('.group-count')?.textContent).toBe('3');
	});

	it('opening a group lists its accounts, tags the one in use and is remembered', async () => {
		snapshot();
		const container = await open();
		const subs = group(container, 'group:subscriptions');
		await fireEvent.click(subs.header);
		expect(subs.header.getAttribute('aria-expanded')).toBe('true');
		expect(subs.sections()).toContain('Claude');
		expect(subs.el.querySelector('.section-in-use')?.textContent).toBe('in use');
		expect(subs.summary()).toBe('');
		expect(usageSections.current['group:subscriptions']).toBe(true);
		cleanup();
		// A fresh popover opens it again.
		expect(group(await open(), 'group:subscriptions').header.getAttribute('aria-expanded')).toBe('true');
	});

	it('a chat that generated an image on fal shows the fal balance in the collapsed API group', async () => {
		snapshot();
		mocks.state.messages = [falImage()];
		const container = await open();
		expect(group(container, 'group:api').summary()).toBe('fal.ai $8.40 left');
		// The plan the chat runs on still heads the subscriptions group.
		expect(group(container, 'group:subscriptions').summary()).toMatch(/^Claude · Weekly/);
	});

	it('a chat whose tools billed an unread xAI key names that key in the collapsed API group', async () => {
		rateLimit.applySnapshot({
			available: true, fetched_at: T0 / 1000, windows: [], providers: [FAL],
			usage_links: [{
				provider_id: 'sample-xai', provider_name: 'Sample xAI', provider_type: 'xai', kind: 'xai_api',
				note: 'No balance reading for this key', dashboard_url: 'https://console.x.ai/', subscription: false,
			}],
		});
		mocks.state.messages = [{
			...falImage(),
			toolResult: JSON.stringify({ status: 'completed', choice: { source: 'xai', account: 'sample-xai' } }),
		} as ChatMessage];
		const container = await open();
		const apis = group(container, 'group:api');
		// xAI keys carry the Grok brand; with no meter the figure is "—".
		expect(apis.summary()).toBe('Grok —');
		await fireEvent.click(apis.header);
		expect(apis.el.querySelector('.section-in-use')).not.toBeNull();
	});

	it('a fal charge on a chat another worker runs is not pinned to this worker\'s fal balance', async () => {
		snapshot();
		mocks.target = 'other-worker';
		mocks.state.messages = [falImage()];
		const container = await open();
		expect(group(container, 'group:api').summary()).toBe('');
	});

	it('a fal model another worker ran is not pinned to this worker\'s fal balance, even on a local chat', async () => {
		const falOn = (worker: string): ChatMessage => ({
			...falImage(),
			toolResult: JSON.stringify({ status: 'completed', choice: { source: 'fal', account: 'fal-ai', worker } }),
		} as ChatMessage);
		// A networked install's payload: per-install snapshots plus its own ids.
		rateLimit.applySnapshot({
			available: true, fetched_at: T0 / 1000, windows: [], providers: [FAL, KIMI_API], usage_links: [],
			self_instance_id: 'install-here', self_device_id: 'device-here',
			snapshots: [{ instance_id: 'install-here', available: false, windows: [] }],
		});
		mocks.state.messages = [falOn('device-other')];
		expect(group(await open(), 'group:api').summary()).toBe('');
		cleanup();
		// The same charge run on this worker names its balance.
		mocks.state.messages = [falOn('device-here')];
		expect(group(await open(), 'group:api').summary()).toBe('fal.ai $8.40 left');
		cleanup();
		// A fal-only tool names no worker: on a networked install the chat may
		// have moved here after it ran elsewhere, so it is not pinned.
		mocks.state.messages = [{
			...falImage(), toolName: 'mcp__ai-tools__editImage',
			toolResult: JSON.stringify({ success: true, provider_used: 'fal-ai' }),
		} as ChatMessage];
		expect(group(await open(), 'group:api').summary()).toBe('');
	});

	it('Claude on a cloud provider (API auth, "Cloud") sits under API keys, not Subscriptions', async () => {
		mocks.claudeAuth = { auth_kind: 'api', plan_type: null };
		snapshot();
		const container = await open();
		const apis = group(container, 'group:api');
		await fireEvent.click(apis.header);
		expect(apis.sections()).toContain('Claude');
		// With Claude gone, nothing is a subscription, so that group is absent.
		expect(group(container, 'group:subscriptions').el).toBeNull();
		// The chat runs on Claude, so the collapsed API group shows it.
		await fireEvent.click(apis.header);
		expect(apis.summary()).toMatch(/^Claude/);
	});

	it('a chat on an API key shows that key, not a tool account, and no plan', async () => {
		snapshot();
		mocks.state.liveProvider = 'kimi-api';
		mocks.state.messages = [falImage()];
		const container = await open();
		expect(group(container, 'group:api').summary()).toBe('Sample Kimi API $12.50 left');
		expect(group(container, 'group:subscriptions').summary()).toBe('');
	});

	it('the OpenAI API key has no meter: "—" collapsed, its console link when opened', async () => {
		snapshot();
		mocks.state.liveProvider = 'sample-openai';
		const container = await open();
		const apis = group(container, 'group:api');
		expect(apis.summary()).toMatch(/^OpenAI.* —$/);
		await fireEvent.click(apis.header);
		const link = apis.el.querySelector(`a[href="${OPENAI_USAGE}"]`) as HTMLAnchorElement;
		expect(link).not.toBeNull();
		expect(link.target).toBe('_blank');
		expect(apis.el.textContent).toContain('Credit balance is shown only on the OpenAI Platform');
	});
});
