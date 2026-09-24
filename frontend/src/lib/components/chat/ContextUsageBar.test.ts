/**
 * ContextUsageBar — render + interaction behaviour for the MERGED pill.
 *
 * One glass pill (context-window ring only) opens ONE popover with two
 * sections: the always-shown context-window breakdown and the resolved
 * install's plan usage limits. The pill is ALWAYS visible; the limits section
 * shows the resolved bucket's windows, or "No usage data yet" when empty.
 *
 * The `rateLimit` store is a singleton; each test resets it via
 * `__resetForTests` and clears localStorage between tests.
 */
import { render, cleanup, fireEvent, waitFor, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ContextUsageBar from './ContextUsageBar.svelte';
import { rateLimit } from '$lib/stores/rate-limit';
import { contextBreakdown, usageSections, hiddenUsageProviders } from '$lib/stores/expansion-preferences';
import type { PreferenceAdapter } from '$lib/services/preference-sync';

// The breakdown now fetches on session load and the pill prefers the rich data, so
// the tests need to drive both the active session state and the /context-usage
// fetch. A tiny controllable store replaces `activeSession` (usage-target reads its
// `.current` + `.subscribe`); `getContextUsage` is a stub. Everything else in the
// active-session / sessions modules is preserved via importOriginal.
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
	function store(initial: State) {
		let val = initial;
		const subs = new Set<(v: State) => void>();
		return {
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
		};
	}
	return {
		adapters: new Map<string, PreferenceAdapter>(),
		save: vi.fn(),
		DEFAULT,
		activeSession: store({ ...DEFAULT }),
		getContextUsage: vi.fn(),
		// Stands in for the hydrated registry's native-Claude auth block; the
		// real one needs a live `GET /models`. Reset in beforeEach, so every
		// other test in this file sees the un-hydrated (all-null) state.
		claudeAuth: { auth_kind: null as string | null, plan_type: null as string | null },
		claudeAuthenticated: false,
	};
});

vi.mock('$lib/services/preferences', () => ({ preferenceSync: {
 register: (key: string, adapter: PreferenceAdapter) => mocks.adapters.set(key, adapter), save: mocks.save
} }));

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
	return { ...actual, getClaudeAuth: () => mocks.claudeAuth };
});
vi.mock('$lib/stores/auth.svelte', async (importOriginal) => {
	const actual = await importOriginal<Record<string, unknown>>();
	return { ...actual, claudeAuthenticated: {
		subscribe(fn: (value: boolean) => void) {
			fn(mocks.claudeAuthenticated);
			return () => {};
		},
	} };
});

const future = () => new Date(Date.now() + 3_600_000).toISOString();
const past = () => new Date(Date.now() - 1_000).toISOString();

function openPopover(container: HTMLElement) {
	const pill = container.querySelector('.usage-indicator') as HTMLButtonElement;
	return fireEvent.click(pill);
}

beforeEach(() => {
	vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
	localStorage.clear();
	mocks.adapters.get('context_breakdown_open')!.apply(undefined);
	mocks.adapters.get('context_usage_sections')!.apply(undefined);
	mocks.adapters.get('context_usage_hidden_providers')!.apply(undefined);
	mocks.save.mockClear();
	// Full reset: clears every bucket AND the per-install generation guard.
	rateLimit.__resetForTests();
	mocks.activeSession.set({ ...mocks.DEFAULT });
	mocks.getContextUsage.mockReset();
	mocks.getContextUsage.mockResolvedValue({ available: false, reason: 'not_connected' });
	mocks.claudeAuth = { auth_kind: null, plan_type: null };
	mocks.claudeAuthenticated = false;
});
afterEach(() => cleanup());

describe('ContextUsageBar — updates while closed', () => {
	it.each([false, true])('animates snapshot balances and live quota on reopen (expanded=%s)', async (expanded) => {
		let callbacks = new Map<number, FrameRequestCallback>();
		let id = 0;
		vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
			callbacks.set(++id, callback); return id;
		});
		vi.stubGlobal('cancelAnimationFrame', (key: number) => callbacks.delete(key));
		vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
		const advance = (time: number) => {
			const pending = [...callbacks.values()]; callbacks.clear();
			pending.forEach(callback => callback(time));
		};
		try {
			usageSections.set({ plan: expanded, 'provider:credits': expanded });
			const snapshot = (balance: number) => rateLimit.applySnapshot({
				available: true, windows: [{ type: 'five_hour', utilization: 24, resets_at: future() }],
				providers: [{ provider_id: 'credits', provider_type: 'fal', provider_name: 'fal.ai',
					credits: { balance_remaining: balance } }]
			});
			snapshot(22.91);
			const { container } = render(ContextUsageBar);
			await openPopover(container);
			expect(container.textContent).toContain('24% left');
			expect(container.textContent).toContain('$22.91 left');
			await openPopover(container);
			snapshot(21.50);
			snapshot(20.44);
			rateLimit.applyLiveEvent({ rate_limit_type: 'five_hour', utilization: 0.82, resets_at: future() });
			await openPopover(container);
			expect(container.textContent).toContain('24% left');
			expect(container.textContent).toContain('$22.91 left');
			advance(0); advance(450);
			expect(container.textContent).toContain('$22.91 left');
			expect(container.querySelectorAll('.usage-number-flipping')).toHaveLength(2);
			expect(container.textContent).not.toContain('$20.44 left');
			advance(1650);
			expect(container.textContent).toContain('18% left');
			expect(container.textContent).toContain('$20.44 left');
			await openPopover(container); await openPopover(container);
			expect(container.textContent).toContain('18% left');
			expect(container.textContent).toContain('$20.44 left');
			expect(callbacks.size).toBe(0);
			// The refresh request started by opening can land after the labels mount.
			snapshot(19.25);
			await waitFor(() => expect(callbacks.size).toBeGreaterThan(0));
			expect(container.textContent).toContain('$20.44 left');
			advance(0); advance(1650);
			expect(container.textContent).toContain('$19.25 left');
		} finally {
			cleanup(); vi.unstubAllGlobals();
		}
	});
});

describe('ContextUsageBar — popover positioning', () => {
	it('updates mounted expansion controls from remote preferences without closing the popover or echoing', async () => {
		rateLimit.applySnapshot({ available: true, windows: [] });
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		mocks.adapters.get('context_breakdown_open')!.apply(false);
		mocks.adapters.get('context_usage_sections')!.apply({ plan: false });
		await waitFor(() => expect(container.querySelector('.ctx-header')?.getAttribute('aria-expanded')).toBe('false'));
		expect(container.querySelector('.usage-popover')).not.toBeNull();
		expect(container.querySelector('.sec-header')?.getAttribute('aria-expanded')).toBe('false');
		expect(mocks.save).not.toHaveBeenCalled();
		mocks.adapters.get('context_breakdown_open')!.apply(undefined);
		mocks.adapters.get('context_usage_sections')!.apply(undefined);
		await waitFor(() => expect(container.querySelector('.ctx-header')?.getAttribute('aria-expanded')).toBe('true'));
		expect(container.querySelector('.sec-header')?.getAttribute('aria-expanded')).toBe('true');
	});


	it('stays inside the chat column when the browser panel resizes it', async () => {
		const originalWidth = window.innerWidth;
		let columnWidth = 600;
		const column = document.createElement('div');
		column.className = 'chat-view';
		document.body.append(column);
		let resize: () => void = () => {};
		const observe = vi.fn();
		const disconnect = vi.fn();
		vi.stubGlobal('ResizeObserver', class {
			constructor(callback: () => void) { resize = callback; }
			observe = observe;
			disconnect = disconnect;
		});
		const rectSpy = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect');
		rectSpy.mockImplementation(function (this: HTMLElement) {
			if (this === column) return DOMRect.fromRect({ x: 240, width: columnWidth });
			const shift = Number(this.style.transform.match(/translateX\((-?[\d.]+)px\)/)?.[1] ?? 0);
			const width = Math.min(380, Number.parseFloat(this.style.getPropertyValue('--usage-popover-room')) || 380);
			return DOMRect.fromRect({ x: 790 - width + shift, width, height: 200 });
		});
		try {
			window.innerWidth = 1440;
			const { container, unmount } = render(ContextUsageBar, { target: column });
			await openPopover(container);
			const popover = container.querySelector('.usage-popover') as HTMLElement;
			expect(observe).toHaveBeenCalledWith(column);
			expect(observe).toHaveBeenCalledWith(popover);
			for (const width of [600, 420, 280, 600]) {
				columnWidth = width;
				for (let repeat = 0; repeat < 2; repeat++) {
					resize();
					await waitFor(() => {
						const rect = popover.getBoundingClientRect();
						expect(rect.left).toBeGreaterThanOrEqual(252);
						expect(rect.right).toBeLessThanOrEqual(240 + width - 12);
						expect(popover.style.getPropertyValue('--usage-popover-room')).toBe(`${width - 24}px`);
					});
				}
			}
			await openPopover(container);
			expect(disconnect).toHaveBeenCalled();
			await unmount();
		} finally {
			rectSpy.mockRestore();
			vi.unstubAllGlobals();
			window.innerWidth = originalWidth;
			column.remove();
		}
	});

	it('keeps translated bounds stable through repeated desktop and mobile resizes', async () => {
		const originalWidth = window.innerWidth;
		let unshiftedLeft = 1201;
		let width = 373;
		const rectSpy = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect');
		rectSpy.mockImplementation(function (this: HTMLElement) {
			const shift = Number(this.style.transform.match(/translateX\((-?[\d.]+)px\)/)?.[1] ?? 0);
			return DOMRect.fromRect({ x: unshiftedLeft + shift, y: 0, width, height: 200 });
		});
		try {
			window.innerWidth = 1280;
			const { container } = render(ContextUsageBar);
			await openPopover(container);
			const popover = container.querySelector('.usage-popover') as HTMLElement;
			const expectBounds = async (shift: number, left: number) => {
				await waitFor(() => expect(popover.style.transform).toBe(`translateX(${shift}px)`));
				const rect = popover.getBoundingClientRect();
				expect(rect.left).toBe(left);
				expect(rect.left).toBeGreaterThanOrEqual(12);
				expect(rect.right).toBeLessThanOrEqual(window.innerWidth - 12);
			};
			await expectBounds(-306, 895);
			// The browser returns already-translated bounds, including on a second
			// resize with unchanged geometry. Never lose that existing translation.
			for (const layout of [
				{ viewport: 1280, left: 1201, width: 373, shift: -306, translatedLeft: 895 },
				{ viewport: 390, left: 327, width: 366, shift: -315, translatedLeft: 12 },
				{ viewport: 1280, left: 1201, width: 373, shift: -306, translatedLeft: 895 },
				{ viewport: 1280, left: 40, width: 373, shift: 0, translatedLeft: 40 },
			]) {
				window.innerWidth = layout.viewport;
				unshiftedLeft = layout.left;
				width = layout.width;
				for (let repeat = 0; repeat < 2; repeat++) {
					await fireEvent(window, new Event('resize'));
					await expectBounds(layout.shift, layout.translatedLeft);
				}
			}
		} finally {
			rectSpy.mockRestore();
			window.innerWidth = originalWidth;
		}
	});
});

describe('ContextUsageBar — pill', () => {
	it('always renders the pill as a bare progress ring (no inline percent text)', () => {
		const { container } = render(ContextUsageBar);
		const pill = container.querySelector('.usage-indicator') as HTMLButtonElement;
		expect(pill).not.toBeNull();
		const ring = container.querySelector('svg.usage-ring');
		expect(ring).not.toBeNull();
		// Background track + progress circle.
		expect(ring?.querySelectorAll('circle').length).toBe(2);
		// The percent text span is gone — the percentage rides on the aria-label.
		expect(container.querySelector('.usage-text')).toBeNull();
		expect(pill.getAttribute('aria-label')).toMatch(/\d+%/);
	});

	it('renders the pill even when no usage windows are known', () => {
		const { container } = render(ContextUsageBar);
		expect(container.querySelector('.usage-indicator')).not.toBeNull();
	});

	it('sources the pill title denominator from the rich breakdown, not the local model context max', async () => {
		mocks.getContextUsage.mockResolvedValue({
			available: true,
			source: 'live',
			usage: {
				categories: [
					{ name: 'Messages', tokens: 180_000, color: '#333' },
					{ name: 'Free space', tokens: 820_000, color: '#444' },
				],
				totalTokens: 180_000,
				maxTokens: 1_000_000,
				rawMaxTokens: 1_000_000,
				percentage: 18,
				model: 'claude-x',
				isAutoCompactEnabled: true,
			},
		});
		// Pin the local model to haiku (200k) — sonnet/opus/fable are natively 1M,
		// which would make this assertion pass for the wrong reason.
		mocks.activeSession.set({ ...mocks.DEFAULT, sessionId: 's1', modelOverride: 'haiku' });
		const { container } = render(ContextUsageBar);
		const pill = container.querySelector('.usage-indicator') as HTMLButtonElement;

		// The local model (haiku) has a 200k window — if the title fell back to it,
		// it would read "200k". It must read the breakdown's 1M instead.
		await waitFor(() => expect(pill.getAttribute('title')).toContain('1M'));
		expect(pill.getAttribute('title')).not.toContain('200k');
	});
});

describe('ContextUsageBar — provider refresh status', () => {
	it.each(
		(['stale', 'unavailable', 'never_fetched'] as const).flatMap((status) =>
			([null, 'timeout', 'unavailable'] as const).flatMap((lastErrorKind) =>
				[false, true].map((cached) => ({ status, lastErrorKind, cached }))
			)
		)
	)('omits refresh narration for $status / $lastErrorKind (cached: $cached)', async ({ status, lastErrorKind, cached }) => {
		rateLimit.applySnapshot({ windows: [], available: false, providers: [
			{ provider_id: 'api', provider_type: 'openai', provider_name: 'OpenAI', status,
				last_error_kind: lastErrorKind,
				...(cached ? { credits: { balance_remaining: 12.34 },
					windows: [{ type: 'five_hour', utilization: 42, resets_at: future() }] } : {}) },
		] });
		const { container, getByRole } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.provider-status')).toBeNull();
		expect(container.textContent).not.toMatch(/Last known|Refresh pending|Refresh timed out|Usage unavailable/);
		expect(container.querySelector('.provider-billing-link')).toBeNull();
		if (cached) {
			expect(container.textContent).toContain('$12.34 left');
			expect(container.textContent).toContain('42%');
			expect(container.textContent).toMatch(/resets in/);
		} else {
			expect(container.textContent).not.toContain('Claude');
			expect(container.textContent).not.toContain('$0.00');
		}
		await fireEvent.click(getByRole('button', { name: 'OpenAI usage — collapse' }));
		expect(container.querySelector('.provider-status')).toBeNull();
		if (cached) expect(container.textContent).toContain('$12.34 left');
	});

	it.each(['stale', 'unavailable', 'never_fetched'] as const)('preserves sign-in guidance for %s credentials', async (status) => {
		rateLimit.applySnapshot({ windows: [], available: false, providers: [
			{ provider_id: 'subscription', provider_type: 'chatgpt', status, last_error_kind: 'missing_credentials' },
		] });
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.provider-status')?.textContent).toBe('Sign in to view usage');
		expect(container.textContent).not.toContain('Last known usage');
	});


	it('labels official organization costs as spend rather than credit remaining', async () => {
		rateLimit.applySnapshot({ windows: [], available: true, providers: [
			{ provider_id: 'admin-api', provider_type: 'openai', provider_name: 'OpenAI', status: 'fresh',
				credits: { spend_month: 12.34, input_tokens: 20, output_tokens: 30, num_model_requests: 2 } },
		] });
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.textContent).toContain('$12.34 spent this month (UTC)');
		expect(container.textContent).not.toContain('$12.34 left');
	});

	it.each(['unavailable', 'stale'] as const)('links to Platform billing when OpenAI billing is %s', async (status) => {
		rateLimit.applySnapshot({ windows: [], available: false, providers: [
			{ provider_id: 'api', provider_type: 'openai', provider_name: 'OpenAI', status,
				last_error_kind: 'billing_access_denied',
				...(status === 'stale' ? { credits: { balance_remaining: 12.34 }, fetched_at: 123 } : {}) },
		] });
		const { container, getByRole } = render(ContextUsageBar);
		await openPopover(container);
		// Keep long guidance in the wrapping prose class, not just an account label.
		const guidance = container.querySelector('.provider-status');
		expect(guidance?.textContent).toBe('Admin usage access refused; polling paused');
		expect(container.textContent).not.toContain('Last known usage');
		expect(container.textContent).not.toContain('Sign in');
		expect(container.textContent).not.toContain('$0.00');
		if (status === 'stale') {
			expect(container.textContent).toContain('$12.34 left');
		}
		const link = getByRole('link', { name: 'View balance in OpenAI Platform billing' });
		expect(link.getAttribute('href')).toBe('https://platform.openai.com/settings/organization/billing/overview');
		expect(link.getAttribute('target')).toBe('_blank');
		expect(link.getAttribute('rel')).toBe('noopener noreferrer');
		expect(link.closest('button')).toBeNull();
		await fireEvent.click(getByRole('button', { name: 'OpenAI usage — collapse' }));
		expect(getByRole('link', { name: 'View balance in OpenAI Platform billing' })).toBe(link);
		if (status === 'stale') expect(container.textContent).toContain('$12.34 left');
	});

	it.each([12.34, 0])('preserves a successful OpenAI API balance of %s', async (balance) => {
		rateLimit.applySnapshot({ windows: [], available: true, providers: [
			{ provider_id: 'api', provider_type: 'openai', provider_name: 'OpenAI', status: 'fresh',
				credits: { balance_remaining: balance } },
		] });
		const { container, getByRole } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.textContent).toContain(`$${balance.toFixed(2)} left`);
		expect(container.querySelector('.provider-billing-link')).toBeNull();
		expect(container.textContent).not.toContain('cannot read');
		await fireEvent.click(getByRole('button', { name: 'OpenAI usage — collapse' }));
		expect(container.textContent).toContain(`$${balance.toFixed(2)} left`);
	});

	it('requests an API credential rather than ChatGPT sign-in for missing OpenAI credentials', async () => {
		rateLimit.applySnapshot({ windows: [], available: false, providers: [
			{ provider_id: 'api', provider_type: 'openai', status: 'unavailable', last_error_kind: 'missing_credentials' },
			{ provider_id: 'other', provider_type: 'openrouter', status: 'unavailable', last_error_kind: 'billing_access_denied' },
		] });
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.textContent).toContain('Configure an Admin API key in provider settings');
		expect(container.textContent).not.toContain('Sign in');
		expect(container.textContent).toContain('Billing access required');
		expect(container.querySelector('.provider-billing-link')).toBeNull();
	});

	it('keeps stale API balances and actionable billing errors without refresh narration', async () => {
		rateLimit.applySnapshot({ windows: [], available: false, providers: [
			{ provider_id: 'api', provider_type: 'openai', provider_name: 'OpenAI', status: 'stale', last_error_kind: 'timeout', credits: { balance_remaining: 12.34 }, fetched_at: 123 },
			{ provider_id: 'other', provider_type: 'openai', provider_name: 'Other API', status: 'unavailable', last_error_kind: 'billing_access_denied' },
		] });
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.textContent).toContain('$12.34');
		expect(container.textContent).not.toMatch(/Last known usage|Refresh timed out/);
		expect(container.textContent).toContain('Admin usage access refused; polling paused');
		expect(container.textContent).not.toContain('$0.00');
	});
});

describe('ContextUsageBar — context breakdown', () => {
	it('renders the collapse chevron with category rows expanded by default', async () => {
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		const header = container.querySelector('.ctx-header');
		expect(header).not.toBeNull();
		expect(container.querySelector('.ctx-chevron')).not.toBeNull();
		expect(header?.getAttribute('aria-expanded')).toBe('true');
		// Expanded by default → the per-category rows are shown.
		expect(container.querySelectorAll('.breakdown-row').length).toBeGreaterThan(0);
	});

	it('collapses on toggle: hides rows, collapses the bar to a single fill, keeps limits, persists to localStorage', async () => {
		rateLimit.applySnapshot({ available: true, windows: [] });
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		const header = container.querySelector('.ctx-header') as HTMLButtonElement;

		await fireEvent.click(header);

		// Rows hidden; the segmented bar collapses to a single severity-coloured
		// fill; the plan-limits section (data rows / "No usage data yet") stays
		// visible even though the "Plan usage limits" title is gone.
		expect(container.querySelectorAll('.breakdown-row').length).toBe(0);
		const segs = container.querySelectorAll('.ctx-seg');
		expect(segs.length).toBe(1);
		expect(segs[0].classList.contains('solid')).toBe(true);
		expect(container.textContent).not.toContain('Plan usage limits');
		expect(container.textContent).toContain('No usage data yet');
		expect(header.getAttribute('aria-expanded')).toBe('false');
		expect(localStorage.getItem('aihub.contextBreakdownOpen')).toBe('0');
	});

	it('starts collapsed when the shared preference says so (default is expanded)', async () => {
		contextBreakdown.set(false);
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.ctx-header')?.getAttribute('aria-expanded')).toBe('false');
		expect(container.querySelectorAll('.breakdown-row').length).toBe(0);
		// Collapsed: a single severity-coloured fill, not the per-category segments.
		const segs = container.querySelectorAll('.ctx-seg');
		expect(segs.length).toBe(1);
		expect(segs[0].classList.contains('solid')).toBe(true);
	});

	it('collapsed bar renders a single severity-coloured fill sized to the total percent', async () => {
		mocks.getContextUsage.mockResolvedValue({
			available: true,
			source: 'live',
			usage: {
				categories: [
					{ name: 'Messages', tokens: 190_000, color: '#333' },
					{ name: 'Free space', tokens: 10_000, color: '#444' },
				],
				totalTokens: 190_000,
				maxTokens: 200_000,
				rawMaxTokens: 200_000,
				percentage: 95,
				model: 'claude-x',
				isAutoCompactEnabled: true,
			},
		});
		mocks.activeSession.set({ ...mocks.DEFAULT, sessionId: 's1' });
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		await waitFor(() => expect(container.querySelector('.ctx-pct')?.textContent).toContain('95%'));

		const header = container.querySelector('.ctx-header') as HTMLButtonElement;
		await fireEvent.click(header); // collapse

		const segs = Array.from(container.querySelectorAll('.ctx-seg'));
		expect(segs.length).toBe(1);
		expect(segs[0].classList.contains('solid')).toBe(true);
		// 95% → danger severity (usageSeverity: >90 is danger).
		expect(segs[0].classList.contains('danger')).toBe(true);
		expect(parseFloat((segs[0] as HTMLElement).style.width)).toBeCloseTo(95, 5);
	});

	it('shows em-dash rows for a fresh session with no per-row mini bars', async () => {
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		// Exclude the trailing Model row (always present when expanded) — this
		// assertion is only about the em-dash category placeholder rows.
		const rows = Array.from(container.querySelectorAll('.breakdown-row:not(.model-row)'));
		// input / cacheRead / cacheCreation / output — no System overhead, no Free space.
		expect(rows.length).toBe(4);
		for (const r of rows) {
			expect(r.querySelector('.breakdown-value')?.textContent?.trim()).toBe('—');
		}
		// The former per-row mini bars are gone from the context section.
		expect(container.querySelector('.row-bar.mini')).toBeNull();
		expect(container.querySelector('.breakdown-row .row-bar')).toBeNull();
	});

	it('renders no bar segments at all on a fresh session (breakdown expanded)', async () => {
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.ctx-header')?.getAttribute('aria-expanded')).toBe('true');
		// Nothing is consumed yet and the autocompact reserve is not counted, so the
		// track is bare.
		expect(container.querySelectorAll('.ctx-seg').length).toBe(0);
	});

	it('renders one segment per in-bar category, excluding Free space AND Output tokens (breakdown expanded)', async () => {
		mocks.activeSession.set({
			...mocks.DEFAULT,
			sessionId: 's1',
			contextUsed: 100_000,
			totalTokensIn: 20_000,
			totalTokensOut: 5_000,
			totalCacheCreationTokens: 30_000,
			totalCacheReadTokens: 50_000,
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.ctx-header')?.getAttribute('aria-expanded')).toBe('true');

		const segs = Array.from(container.querySelectorAll('.ctx-seg'));
		// Input + cacheRead + cacheCreation segments only. Output tokens are
		// session-cumulative and NOT part of totalUsed, so — unlike Free space,
		// which is the bare track — it renders no segment at all.
		expect(segs.length).toBe(3);
		// The rows still include both Output tokens and Free space (5 category rows
		// total, plus the trailing Model row), but neither is a segment.
		expect(container.querySelectorAll('.breakdown-row:not(.model-row)').length).toBe(5);
		// Segment widths (as fractions of contextMax) sum to the same total the
		// header reports — the bar-sum invariant.
		const contextMax = 1_000_000; // default model (sonnet) is natively 1M
		const segTokens = Math.round(
			segs.reduce(
				(sum, s) => sum + (parseFloat((s as HTMLElement).style.width) / 100) * contextMax,
				0
			)
		);
		expect(segTokens).toBe(20_000 + 50_000 + 30_000); // input + cacheRead + cacheCreation
		expect(segTokens).toBe(100_000); // === totalUsed / the header total
	});

	it('renders a deferred category row with tokens + em-dash (no percent) and no bar segment (breakdown expanded)', async () => {
		mocks.getContextUsage.mockResolvedValue({
			available: true,
			source: 'live',
			usage: {
				categories: [
					{ name: 'Messages', tokens: 76_000, color: '#333' },
					{ name: 'MCP tools (deferred)', tokens: 40_500, color: '#222', isDeferred: true },
					{ name: 'Free space', tokens: 83_500, color: '#444' },
				],
				totalTokens: 76_000,
				maxTokens: 200_000,
				rawMaxTokens: 200_000,
				percentage: 38,
				model: 'claude-x',
				isAutoCompactEnabled: true,
			},
		});
		mocks.activeSession.set({ ...mocks.DEFAULT, sessionId: 's1' });
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		await waitFor(() => expect(container.textContent).toContain('MCP tools (deferred)'));
		expect(container.querySelector('.ctx-header')?.getAttribute('aria-expanded')).toBe('true');

		const rows = Array.from(container.querySelectorAll('.breakdown-row'));
		const deferredRow = rows.find((r) => r.textContent?.includes('MCP tools (deferred)'))!;
		expect(deferredRow).toBeDefined();
		expect(deferredRow.classList.contains('muted')).toBe(true);
		const value = deferredRow.querySelector('.breakdown-value')?.textContent ?? '';
		// Tokens are shown ("40.5k") alongside an em-dash instead of a percent.
		expect(value).toContain('40.5k');
		expect(value).toContain('—');
		expect(value).not.toMatch(/\d+(\.\d+)?%/);

		// No segmented-bar segment for the deferred category: Messages (in-bar) is
		// the only segment.
		expect(container.querySelectorAll('.ctx-seg').length).toBe(1);
	});

	it('never leaves a stale breakdown on screen when the session switches mid-fetch (abort race)', async () => {
		const deferred: { resolve: ((v: unknown) => void) | null } = { resolve: null };
		mocks.getContextUsage.mockImplementation((sid: string) => {
			if (sid === 's1') {
				return new Promise<unknown>((resolve) => {
					deferred.resolve = resolve;
				});
			}
			return Promise.resolve({
				available: true,
				source: 'live',
				usage: {
					categories: [
						{ name: 'Messages', tokens: 10_000, color: '#333' },
						{ name: 'Free space', tokens: 190_000, color: '#444' },
					],
					totalTokens: 10_000,
					maxTokens: 200_000,
					rawMaxTokens: 200_000,
					percentage: 5,
					model: 'claude-x',
					isAutoCompactEnabled: true,
				},
			});
		});

		mocks.activeSession.set({ ...mocks.DEFAULT, sessionId: 's1' });
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		// Switch sessions before the s1 fetch resolves — the effect aborts s1's
		// in-flight controller and kicks off a fetch for s2.
		mocks.activeSession.set({ ...mocks.DEFAULT, sessionId: 's2' });
		await waitFor(() => expect(container.textContent).toContain('Messages'));

		// Now let the stale s1 promise resolve — its continuation must be a no-op
		// (aborted-signal guard) and must NOT overwrite s2's breakdown.
		deferred.resolve?.({
			available: true,
			source: 'live',
			usage: {
				categories: [
					{ name: 'Old session data', tokens: 199_000, color: '#111' },
					{ name: 'Free space', tokens: 1_000, color: '#444' },
				],
				totalTokens: 199_000,
				maxTokens: 200_000,
				rawMaxTokens: 200_000,
				percentage: 99,
				model: 'claude-x',
				isAutoCompactEnabled: true,
			},
		});
		await Promise.resolve();
		await Promise.resolve();

		expect(container.textContent).not.toContain('Old session data');
		expect(container.textContent).toContain('Messages');
	});

	it('drives the pill ring from the rich breakdown percent once loaded (pill and header agree)', async () => {
		mocks.getContextUsage.mockResolvedValue({
			available: true,
			source: 'live',
			usage: {
				categories: [
					{ name: 'Messages', tokens: 180_000, color: '#333' },
					{ name: 'Free space', tokens: 20_000, color: '#444' },
				],
				totalTokens: 180_000,
				maxTokens: 200_000,
				rawMaxTokens: 200_000,
				percentage: 90,
				model: 'claude-x',
				isAutoCompactEnabled: true,
			},
		});
		mocks.activeSession.set({ ...mocks.DEFAULT, sessionId: 's1' });
		const { container } = render(ContextUsageBar);
		const pill = container.querySelector('.usage-indicator') as HTMLButtonElement;

		// Rich totalPercent = 180k / 200k = 90% — not the ~17% poller estimate.
		await waitFor(() => expect(pill.getAttribute('aria-label')).toMatch(/90%/));
		expect(pill.getAttribute('aria-label')).not.toMatch(/17%/);

		await openPopover(container);
		expect(container.querySelector('.ctx-pct')?.textContent).toContain('90%');
	});

	it('colors the toolbar ring from chat context, not account plan usage', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 2, resets_at: future() }],
		});
		mocks.activeSession.set({
			...mocks.DEFAULT,
			contextUsed: 10_000,
			totalTokensIn: 10_000,
		});

		const { container } = render(ContextUsageBar);
		const pill = container.querySelector('.usage-indicator') as HTMLButtonElement;
		const ring = container.querySelector('svg.usage-ring') as SVGElement;

		expect(ring).not.toBeNull();
		expect(ring.classList.contains('warning')).toBe(false);
		expect(ring.classList.contains('danger')).toBe(false);
		// 10k used over sonnet's native 1M window (no reserve folded in).
		expect(pill.getAttribute('aria-label')).toMatch(/1% of context window used/);

		await openPopover(container);
		const planPct = container.querySelector('.popover-row .row-pct') as HTMLElement;
		expect(planPct.textContent).toBe('2% left');
		expect(planPct.classList.contains('danger')).toBe(true);
	});

	it('shows the effective model as a trailing muted row inside the expanded breakdown', async () => {
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		const modelRow = container.querySelector('.model-row');
		expect(modelRow).not.toBeNull();
		expect(modelRow?.classList.contains('breakdown-row')).toBe(true);
		expect(modelRow?.classList.contains('muted')).toBe(true);
		expect(modelRow?.querySelector('.breakdown-label')?.textContent).toBe('Model');
		// Default model ('sonnet') resolves to the "Sonnet" label.
		expect(modelRow?.querySelector('.breakdown-value')?.textContent).toBe('Sonnet');
		// No swatch and no percent on the model row.
		expect(modelRow?.querySelector('.cat-swatch')).toBeNull();
		expect(modelRow?.querySelector('.cat-pct')).toBeNull();
	});

	it('hides the model row when the breakdown is collapsed', async () => {
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.model-row')).not.toBeNull();

		const header = container.querySelector('.ctx-header') as HTMLButtonElement;
		await fireEvent.click(header);

		expect(container.querySelectorAll('.breakdown-row').length).toBe(0);
		expect(container.querySelector('.model-row')).toBeNull();
	});
});

describe('ContextUsageBar — footer', () => {
	it('never renders a footer — neither on the poller estimate nor once a rich breakdown loads', async () => {
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.popover-footer')).toBeNull();

		mocks.getContextUsage.mockResolvedValue({
			available: true,
			source: 'live',
			usage: {
				categories: [
					{ name: 'Messages', tokens: 10_000, color: '#333' },
					{ name: 'Free space', tokens: 190_000, color: '#444' },
				],
				totalTokens: 10_000,
				maxTokens: 200_000,
				rawMaxTokens: 200_000,
				percentage: 5,
				model: 'claude-x',
				isAutoCompactEnabled: true,
			},
		});
		mocks.activeSession.set({ ...mocks.DEFAULT, sessionId: 's1' });
		await waitFor(() => expect(container.textContent).toContain('Messages'));
		expect(container.querySelector('.popover-footer')).toBeNull();
	});
});

describe('ContextUsageBar — plan usage limits', () => {
	it('hides Claude without an account while keeping other providers visible', async () => {
		// The registry defaults to subscription metadata even without an account.
		mocks.claudeAuth = { auth_kind: 'subscription', plan_type: null };
		rateLimit.applySnapshot({ available: false, windows: [], providers: [
			{ provider_id: 'codex-1', provider_type: 'chatgpt', provider_name: 'ChatGPT',
				windows: [{ type: 'five_hour', utilization: 30, resets_at: future() }] },
		] });
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.ctx-header')).not.toBeNull();
		expect(Array.from(container.querySelectorAll('.section-device'), e => e.textContent)).toEqual(['OpenAI']);
		expect(container.textContent).toContain('70% left');
		expect(container.querySelectorAll('.popover-divider')).toHaveLength(1);
	});

	it('hides Claude before credentials or snapshots are known', async () => {
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.ctx-header')).not.toBeNull();
		expect(container.querySelector('.popover-section')).toBeNull();
		expect(container.querySelector('.popover-divider')).toBeNull();
	});

	it('updates Claude visibility when credentials become available or are removed', async () => {
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		rateLimit.applySnapshot({ available: true, windows: [] });
		await waitFor(() => expect(container.querySelector('.section-device')?.textContent).toBe('Claude'));
		rateLimit.applySnapshot({ available: false, windows: [] });
		await waitFor(() => expect(container.querySelector('.popover-section')).toBeNull());
	});

	it.each(['api', 'subscription'])('shows "No usage data yet" for %s credentials before a usage snapshot arrives', async (authKind) => {
		mocks.claudeAuth = { auth_kind: authKind, plan_type: null };
		mocks.claudeAuthenticated = authKind === 'subscription';
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.usage-popover')).not.toBeNull();
		expect(container.textContent).toContain('No usage data yet');
		expect(container.querySelectorAll('.popover-row').length).toBe(0);
	});

	it('never shows the "Plan usage limits" title, whether the section is empty or has rows', async () => {
		rateLimit.applySnapshot({ available: true, windows: [] });
		const { container: empty } = render(ContextUsageBar);
		await openPopover(empty);
		expect(empty.textContent).not.toContain('Plan usage limits');
		expect(empty.textContent).toContain('No usage data yet');
		cleanup();

		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 42, resets_at: future() }],
		});
		const { container: withRows } = render(ContextUsageBar);
		await openPopover(withRows);
		expect(withRows.textContent).not.toContain('Plan usage limits');
		expect(withRows.querySelectorAll('.popover-row').length).toBe(1);
	});

	it('preserves the Claude feed as quota remaining', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 14, resets_at: future() }],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		const pct = container.querySelector('.popover-row .row-pct') as HTMLElement;
		expect(pct.textContent).toBe('14% left');
		expect(pct.classList.contains('warning')).toBe(true);
		expect(pct.classList.contains('danger')).toBe(false);
	});

	it('warns from low remaining Claude quota, not high remaining quota', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 9, resets_at: future() }],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		const pct = container.querySelector('.popover-row .row-pct') as HTMLElement;
		expect(pct.textContent).toBe('9% left');
		expect(pct.classList.contains('danger')).toBe(true);
	});

	it('converts a live SDK used fraction into the remaining percentage', async () => {
		rateLimit.applyLiveEvent({
			status: 'allowed',
			rate_limit_type: 'five_hour',
			utilization: 0.25, // 25% of the window used → 75% left
			resets_at: future(),
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.popover-row .row-pct')?.textContent).toBe('75% left');
	});

	it('shows 100% left once a subscription window has rolled over', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 42, resets_at: past() }],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.popover-row .row-pct')?.textContent).toBe('100% left');
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
		// The Claude section header always renders now (self, unlabeled → just "Claude").
		expect(container.querySelector('.popover-section')).not.toBeNull();
		expect(container.querySelector('.section-device')?.textContent).toBe('Claude');
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
		// Header is "Claude" + the resolved device name; the account email is never shown.
		expect(container.querySelector('.section-device')?.textContent).toBe('Claude');
		expect(container.textContent).toContain('Studio');
		expect(container.textContent).not.toContain('a@example.com');
		expect(container.textContent).not.toContain('Worker');
		expect(container.textContent).not.toContain('b@example.com');
		expect(container.querySelector('.popover-row .row-pct')?.textContent).toBe('12% left');
		expect(container.textContent).not.toContain('88%');
	});

	it('heads the Claude section with the resolved install device label (never the account name/email)', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [
				{
					instance_id: 'primary',
					device_name: 'Studio',
					account_email: 'a@example.com',
					account_name: 'Personal',
					available: true,
					windows: [{ type: 'five_hour', utilization: 12, resets_at: future() }],
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelectorAll('.popover-section').length).toBe(1);
		// Fixed "Claude" label + the resolved install's device name as the sub-label.
		expect(container.querySelector('.section-device')?.textContent).toBe('Claude');
		expect(container.querySelector('.section-account')?.textContent).toBe('Studio');
		// Neither the account email nor the account name leaks into the header.
		expect(container.textContent).not.toContain('a@example.com');
		expect(container.textContent).not.toContain('Personal');
	});

	it('unlabeled self shows just the "Claude" header (no device / "Other device")', async () => {
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
		// The Claude header always renders; unlabeled self → just "Claude", with
		// no device sub-label and no "Other device" hint (this IS the self install).
		expect(container.querySelector('.popover-section')).not.toBeNull();
		expect(container.querySelector('.section-device')?.textContent).toBe('Claude');
		expect(container.querySelector('.section-account')).toBeNull();
		expect(container.textContent).not.toContain('This device');
		expect(container.textContent).not.toContain('Other device');
		// Only the self window is shown (12% left), not the worker's live 50%.
		expect(container.querySelectorAll('.popover-row').length).toBe(1);
		expect(container.textContent).not.toContain('50%');
	});

	it('subtexts the Claude header with the resolved install snapshot plan', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [
				{
					instance_id: 'primary',
					device_name: 'Studio',
					plan_type: 'max',
					available: true,
					windows: [{ type: 'five_hour', utilization: 12, resets_at: future() }],
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.section-device')?.textContent).toBe('Claude');
		// The plan WINS over the device-name fallback — it is the more useful
		// disambiguator, and it is what the model dropdown shows too.
		expect(container.querySelector('.section-account')?.textContent).toBe('max');
	});

	it("prefers the resolved install's snapshot plan over the local registry's", async () => {
		// The snapshot is per-install (it can describe a remote worker on a
		// different account); the registry block only ever describes self. When
		// both carry a plan, the snapshot wins.
		mocks.claudeAuth = { auth_kind: 'subscription', plan_type: 'pro' };
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [
				{
					instance_id: 'primary',
					device_name: 'Studio',
					plan_type: 'max',
					available: true,
					windows: [{ type: 'five_hour', utilization: 12, resets_at: future() }],
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.section-account')?.textContent).toBe('max');
	});

	it('subtexts the self Claude header "API" when the registry reports API-key auth', async () => {
		mocks.claudeAuth = { auth_kind: 'api', plan_type: null };
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
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.section-device')?.textContent).toBe('Claude');
		// "API" beats the device-name fallback: it says HOW this install bills.
		expect(container.querySelector('.section-account')?.textContent).toBe('API');
	});

	it("subtexts the self Claude header with the registry's plan when no snapshot plan exists", async () => {
		mocks.claudeAuth = { auth_kind: 'subscription', plan_type: 'pro' };
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
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.section-account')?.textContent).toBe('pro');
	});

	it('collapses the Claude plan section on header click: hides window rows and persists', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 42, resets_at: future() }],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		// Open by default → the Claude header plus its single window row.
		expect(container.querySelector('.section-device')?.textContent).toBe('Claude');
		expect(container.querySelectorAll('.popover-row').length).toBe(1);

		const secHeader = container.querySelector('.sec-header') as HTMLButtonElement;
		await fireEvent.click(secHeader); // collapse
		expect(container.querySelectorAll('.popover-row').length).toBe(0);
		expect(secHeader.getAttribute('aria-expanded')).toBe('false');
		expect(localStorage.getItem('aihub.usageSectionOpen:plan')).toBe('0');
		// Collapsed → the least remaining plan pct rides on the header summary.
		expect(container.querySelector('.sec-summary')?.textContent).toBe('42% left');

		await fireEvent.click(secHeader); // expand again
		expect(container.querySelectorAll('.popover-row').length).toBe(1);
		expect(secHeader.getAttribute('aria-expanded')).toBe('true');
		expect(localStorage.getItem('aihub.usageSectionOpen:plan')).toBe('1');
	});
});

describe('ContextUsageBar — provider visibility', () => {
	it('hides and restores each provider independently without changing its expansion or usage data', async () => {
		rateLimit.applySnapshot({ available: true, windows: [], providers: [
			{ provider_id: 'api', provider_type: 'openai', provider_name: 'OpenAI', windows: [], credits: { balance_remaining: 12 } },
			{ provider_id: 'other', provider_type: 'openai', provider_name: 'OpenAI', windows: [] }
		] });
		usageSections.set({ 'provider:api': false });
		const { container, getByRole, getAllByRole } = render(ContextUsageBar);
		await openPopover(container);
		await fireEvent.click(getAllByRole('button', { name: 'Hide OpenAI usage' })[0]);
		expect(container.querySelector('.usage-popover')).not.toBeNull();
		expect(getAllByRole('button', { name: 'Hide OpenAI usage' })).toHaveLength(1);
		expect(usageSections.current['provider:api']).toBe(false);
		expect(hiddenUsageProviders.current).toEqual({ 'provider:api': true });
		expect(mocks.save).toHaveBeenLastCalledWith('context_usage_hidden_providers', { 'provider:api': true });
		await fireEvent.click(getByRole('button', { name: 'Hide Claude usage' }));
		expect(container.querySelector('.hidden-providers summary')?.textContent).toBe('Hidden providers (2)');
		await fireEvent.click(container.querySelector('.usage-indicator')!);
		await openPopover(container);
		expect(container.querySelectorAll('.provider-hide')).toHaveLength(1);
		await fireEvent.click(container.querySelector('.hidden-providers summary')!);
		await fireEvent.click(getByRole('button', { name: 'Show OpenAI usage' }));
		expect(getByRole('button', { name: 'OpenAI usage — expand' }).getAttribute('aria-expanded')).toBe('false');
		expect(container.textContent).toContain('$12.00');
		await fireEvent.click(getByRole('button', { name: 'Show Claude usage' }));
		expect(container.querySelector('.hidden-providers')).toBeNull();
		expect(container.querySelectorAll('.provider-hide')).toHaveLength(3);
	});

	it('keeps context and restore controls available when every provider is hidden, and applies remote changes', async () => {
		rateLimit.applySnapshot({ available: true, windows: [], providers: [
			{ provider_id: 'api', provider_type: 'openai', windows: [] }
		] });
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		mocks.adapters.get('context_usage_hidden_providers')!.apply({ plan: true, 'provider:api': true });
		await waitFor(() => expect(container.querySelectorAll('.popover-section')).toHaveLength(0));
		expect(container.querySelector('.ctx-header')).not.toBeNull();
		expect(container.querySelector('.hidden-providers summary')?.textContent).toBe('Hidden providers (2)');
		expect(mocks.save).not.toHaveBeenCalled();
		mocks.adapters.get('context_usage_hidden_providers')!.apply(undefined);
		await waitFor(() => expect(container.querySelectorAll('.provider-hide')).toHaveLength(2));
	});
});

describe('ContextUsageBar — provider usage (non-Claude)', () => {
	it('renders a provider section header + one row per window with the right label/percent', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [
				{
					provider_id: 'codex-1',
					provider_name: 'ChatGPT',
					provider_type: 'chatgpt',
					plan_type: 'prolite',
					windows: [
						{ type: 'five_hour', utilization: 30, resets_at: future() },
						{ type: 'seven_day', utilization: 12, resets_at: future() },
					],
					credits: null,
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		// The Claude header always renders (self, empty windows → just "Claude");
		// the provider adds its own section header below it.
		expect(container.querySelectorAll('.popover-section').length).toBe(2);
		// A ChatGPT-subscription row displays under the OpenAI brand.
		const deviceLabels = Array.from(container.querySelectorAll('.section-device')).map(
			(e) => e.textContent
		);
		expect(deviceLabels).toEqual(['Claude', 'OpenAI']);
		// The only .section-account belongs to the provider header (Claude self is unlabeled).
		expect(container.querySelector('.section-account')?.textContent).toBe('prolite');

		// One row per provider window (the empty Claude section shows a
		// non-.popover-row "No usage data yet" line, not counted here).
		const rows = container.querySelectorAll('.popover-row');
		expect(rows.length).toBe(2);
		expect(rows[0].querySelector('.row-label')?.textContent).toBe('5-hour limit');
		expect(rows[0].querySelector('.row-pct')?.textContent).toBe('70% left');
		expect(rows[1].querySelector('.row-label')?.textContent).toBe('Weekly (all models)');
		expect(rows[1].querySelector('.row-pct')?.textContent).toBe('88% left');
		// Each window row draws a bar fill sized to its percent.
		expect(rows[0].querySelector('.row-fill')).not.toBeNull();
	});

	it('shows high OpenAI subscription quota remaining without warning or danger color', async () => {
		usageSections.set({ 'provider:codex-91': false });
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [
				{
					provider_id: 'codex-91',
					provider_name: 'ChatGPT',
					provider_type: 'chatgpt',
					plan_type: 'prolite',
					windows: [
						{ type: 'five_hour', utilization: 9, resets_at: future() },
						{ type: 'seven_day', utilization: 2, resets_at: future() },
					],
					credits: null,
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		const summary = container.querySelector('.row-pct.sec-summary') as HTMLElement;
		expect(summary.textContent).toBe('91% left');
		expect(summary.classList.contains('warning')).toBe(false);
		expect(summary.classList.contains('danger')).toBe(false);
	});

	it('falls back to the provider type, then "Provider", for the section header', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [
				{
					provider_id: 'p-typed',
					provider_type: 'openrouter',
					windows: [{ type: 'day', utilization: 5, resets_at: future() }],
					credits: null,
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		// [0] is the always-present Claude header; [1] is the provider (type fallback).
		const deviceLabels = Array.from(container.querySelectorAll('.section-device')).map(
			(e) => e.textContent
		);
		expect(deviceLabels).toEqual(['Claude', 'openrouter']);
		// An API-key row is always subtexted "API" (the Claude self header stays
		// unlabeled — no plan data and no device name in this fixture).
		const accounts = Array.from(container.querySelectorAll('.section-account')).map(
			(e) => e.textContent
		);
		expect(accounts).toEqual(['API']);
		expect(container.textContent).toContain('Daily');
	});

	it('renders a Grok subscription section as quota remaining, without Grok Build', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [
				{
					provider_id: 'grok-1',
					provider_name: 'Grok',
					provider_type: 'grok',
					plan_type: 'SuperGrok',
					windows: [
						{ type: 'seven_day', utilization: 34, resets_at: future() },
						{ type: 'grok_api', utilization: 20, resets_at: future() },
						// A stale persisted bucket may still carry this — never rendered.
						{ type: 'grok_build', utilization: 12, resets_at: future() },
					],
					credits: null,
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		expect(container.querySelectorAll('.popover-section').length).toBe(2);
		const deviceLabels = Array.from(container.querySelectorAll('.section-device')).map(
			(e) => e.textContent
		);
		expect(deviceLabels).toEqual(['Claude', 'Grok']);
		expect(container.querySelector('.section-account')?.textContent).toBe('SuperGrok');

		// SuperGrok billing reports used-%, converted to "% left" for display.
		const rows = container.querySelectorAll('.popover-row');
		expect(rows.length).toBe(2);
		expect(rows[0].querySelector('.row-label')?.textContent).toBe('Weekly (all models)');
		expect(rows[0].querySelector('.row-pct')?.textContent).toBe('66% left');
		expect(rows[1].querySelector('.row-label')?.textContent).toBe('API');
		expect(rows[1].querySelector('.row-pct')?.textContent).toBe('80% left');
		expect(container.textContent).not.toContain('Grok Build');
	});

	it('summarizes a collapsed Grok section with the least quota remaining', async () => {
		usageSections.set({ 'provider:grok-2': false });
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [
				{
					provider_id: 'grok-2',
					provider_name: 'Grok',
					provider_type: 'grok',
					plan_type: 'SuperGrok',
					windows: [
						{ type: 'seven_day', utilization: 34, resets_at: future() },
						{ type: 'grok_api', utilization: 70, resets_at: future() },
					],
					credits: null,
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		const summary = container.querySelector('.row-pct.sec-summary') as HTMLElement;
		expect(summary.textContent).toBe('30% left');
	});

	it('subtexts an OpenAI API-key row as "OpenAI · API", separate from the ChatGPT row', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [
				{
					provider_id: 'p-chatgpt',
					provider_name: 'ChatGPT',
					provider_type: 'chatgpt',
					plan_type: 'prolite',
					windows: [{ type: 'five_hour', utilization: 1, resets_at: future() }],
					credits: null,
				},
				{
					provider_id: 'p-openai',
					provider_name: 'OpenAI',
					provider_type: 'openai',
					windows: [{ type: 'requests', utilization: 0, resets_at: future() }],
					credits: null,
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		// Both rows show the OpenAI brand and stay SEPARATE sections; the subtext
		// is what tells them apart.
		const deviceLabels = Array.from(container.querySelectorAll('.section-device')).map(
			(e) => e.textContent
		);
		expect(deviceLabels).toEqual(['Claude', 'OpenAI', 'OpenAI']);
		const accounts = Array.from(container.querySelectorAll('.section-account')).map(
			(e) => e.textContent
		);
		expect(accounts).toEqual(['prolite', 'API']);
		expect(container.querySelectorAll('.popover-section').length).toBe(3);
	});

	it('keeps account and key rows separate without source detail text', async () => {
        rateLimit.applySnapshot({ available: false, windows: [], providers: [
            { provider_id: 'or-key', provider_type: 'openrouter', provider_name: 'OpenRouter',
              credits: { usage: 3, limit: 10 }, source: 'OpenRouter key API', scope: 'This model key spend', fetched_at: 1000 },
            { provider_id: 'openrouter-account:or-key', provider_type: 'openrouter_account', provider_name: 'OpenRouter account',
              credits: { balance_remaining: 20.75 }, source: 'OpenRouter management API', scope: 'Account credit ledger (USD)', fetched_at: 2000 }
        ] });
        const { container } = render(ContextUsageBar);
        await openPopover(container);
        expect(screen.getByRole('button', { name: 'OpenRouter account usage — collapse' })).toBeTruthy();
        expect(container.textContent).not.toContain('Source:');
        expect(container.textContent).not.toContain('Account credit ledger (USD)');
        expect(container.textContent).not.toContain('Last successful update:');
        expect(screen.getByRole('button', { name: 'OpenRouter usage — collapse' })).toBeTruthy();
    });

	it('sorts subscription rows A–Z above API rows A–Z', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [
				{
					provider_id: 'or-1',
					provider_name: 'OpenRouter',
					provider_type: 'openrouter',
					windows: [],
					credits: { balance_remaining: 20.52 }
				},
				{
					provider_id: 'fal-ai',
					provider_name: 'fal.ai',
					provider_type: 'fal',
					windows: [],
					credits: { balance_remaining: 22.91 }
				},
				{
					provider_id: 'p-chatgpt',
					provider_name: 'ChatGPT',
					provider_type: 'chatgpt',
					plan_type: 'prolite',
					windows: [{ type: 'five_hour', utilization: 0, resets_at: future() }],
					credits: { balance: 0 }
				},
				{
					provider_id: 'p-grok',
					provider_name: 'Grok',
					provider_type: 'grok',
					plan_type: 'SuperGrok',
					windows: [{ type: 'seven_day', utilization: 4, resets_at: future() }],
					credits: null
				},
				{
					provider_id: 'openai-api',
					provider_name: 'OpenAI',
					provider_type: 'openai',
					windows: [],
					credits: { balance_remaining: 12.5 }
				}
			]
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		const labels = Array.from(container.querySelectorAll('.section-device')).map(
			(e) => e.textContent
		);
		const accounts = Array.from(container.querySelectorAll('.section-account')).map(
			(e) => e.textContent
		);
		// Claude (native) stays first; then subs A–Z (Grok, OpenAI prolite);
		// then APIs A–Z (fal.ai, OpenAI API, OpenRouter).
		expect(labels).toEqual(['Claude', 'Grok', 'OpenAI', 'fal.ai', 'OpenAI', 'OpenRouter']);
		expect(accounts).toEqual(['SuperGrok', 'prolite', 'API', 'API', 'API']);
	});

	it('does not show Codex extra-credit $0 on the ChatGPT subscription row', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [
				{
					provider_id: 'p-chatgpt',
					provider_name: 'ChatGPT',
					provider_type: 'chatgpt',
					plan_type: 'prolite',
					windows: [{ type: 'five_hour', utilization: 0, resets_at: future() }],
					credits: { has_credits: true, balance: 0 }
				}
			]
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.textContent).not.toContain('$0.00');
		expect(container.textContent).not.toContain('Credits: $');
	});

	it('renders a muted credits line and NO bar for a null-limit provider (OpenRouter-style)', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [
				{
					provider_id: 'or-1',
					provider_name: 'OpenRouter',
					provider_type: 'openrouter',
					windows: [],
					credits: { usage: 3.1, limit_remaining: 6.9, unlimited: false },
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		// [0] is the always-present Claude header; [1] is the OpenRouter provider.
		const deviceLabels = Array.from(container.querySelectorAll('.section-device')).map(
			(e) => e.textContent
		);
		expect(deviceLabels).toEqual(['Claude', 'OpenRouter']);
		// Empty windows → no provider window rows and no bars anywhere (Claude
		// section is also empty).
		expect(container.querySelectorAll('.popover-row').length).toBe(0);
		expect(container.querySelectorAll('.row-bar').length).toBe(0);
		expect(container.querySelectorAll('.row-fill').length).toBe(0);
		// The credits line renders from usage / limit_remaining.
		expect(container.textContent).toContain('$3.10 used · $6.90 left');
	});

	it('renders "Credits: unlimited" when the provider reports unlimited credits', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [
				{
					provider_id: 'codex-unl',
					provider_name: 'Codex',
					windows: [{ type: 'five_hour', utilization: 40, resets_at: future() }],
					credits: { unlimited: true },
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		// A window row (with bar) AND the credits line coexist.
		expect(container.querySelectorAll('.popover-row').length).toBe(1);
		expect(container.textContent).toContain('Credits: unlimited');
	});

	it('collapsed credits-only provider (no windows) shows a compact credits summary on the header', async () => {
		// Persisted-collapsed before render so the header renders in its collapsed state.
		usageSections.set({ 'provider:or-credits': false });
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [
				{
					provider_id: 'or-credits',
					provider_name: 'OpenRouter',
					provider_type: 'openrouter',
					windows: [],
					credits: { usage: 12.34, limit_remaining: 87.66, unlimited: false },
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		// No windows → no pct chip; the credits summary rides the collapsed header.
		const chip = container.querySelector('.sec-credits');
		expect(chip).not.toBeNull();
		expect(chip?.textContent).toBe('$12.34 used');
		// It is a header summary, not a severity pct chip.
		expect(container.querySelector('.row-pct.sec-summary')).toBeNull();
		// Collapsed → the full body credits line is hidden.
		expect(container.textContent).not.toContain('$12.34 used · $87.66 left');
	});

	it('expanded credits-only provider renders the full body credits line and no header chip', async () => {
		// Provider sections default to open — no collapse seeding.
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [
				{
					provider_id: 'or-credits-open',
					provider_name: 'OpenRouter',
					provider_type: 'openrouter',
					windows: [],
					credits: { usage: 12.34, limit_remaining: 87.66, unlimited: false },
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		// Expanded → full credits line in the body, and no collapsed-header chip.
		expect(container.textContent).toContain('$12.34 used · $87.66 left');
		expect(container.querySelector('.sec-credits')).toBeNull();
		expect(container.querySelector('.sec-summary')).toBeNull();
	});

	it('headlines the account balance when the provider reports balance_remaining', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [
				{
					provider_id: 'or-balance',
					provider_name: 'OpenRouter',
					provider_type: 'openrouter',
					windows: [],
					// /key figures (usage / limit_remaining) plus the /credits ledger.
					credits: {
						usage: 3.1,
						limit_remaining: 6.9,
						total_credits: 25,
						total_usage: 4.25,
						balance_remaining: 20.75,
					},
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		// Balance outranks the key-scoped usage/limit_remaining pair.
		expect(container.textContent).toContain('$20.75 left · $4.25 used');
		expect(container.textContent).not.toContain('$3.10 used · $6.90 left');
	});

	it('collapsed balance provider shows a compact "$X left" chip', async () => {
		usageSections.set({ 'provider:or-balance-collapsed': false });
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [
				{
					provider_id: 'or-balance-collapsed',
					provider_name: 'OpenRouter',
					provider_type: 'openrouter',
					windows: [],
					credits: { usage: 3.1, total_usage: 4.25, balance_remaining: 20.75 },
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		expect(container.querySelector('.sec-credits')?.textContent).toBe('$20.75 left');
	});

	it('falls back to the usage/limit line when balance_remaining is absent or non-numeric', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [
				{
					provider_id: 'or-no-balance',
					provider_name: 'OpenRouter',
					provider_type: 'openrouter',
					windows: [],
					// A malformed blob must not swallow the existing behaviour.
					credits: { usage: 3.1, limit_remaining: 6.9, balance_remaining: 'oops' },
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		expect(container.textContent).toContain('$3.10 used · $6.90 left');
	});

	it('collapsed provider WITH windows keeps the % chip (not the credits summary)', async () => {
		usageSections.set({ 'provider:or-both': false });
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [
				{
					provider_id: 'or-both',
					provider_name: 'OpenRouter',
					provider_type: 'openrouter',
					windows: [{ type: 'five_hour', utilization: 30, resets_at: future() }],
					credits: { usage: 12.34, limit_remaining: 87.66, unlimited: false },
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		// A window exists → the max-window pct chip wins; credits fallback stays hidden.
		expect(container.querySelector('.row-pct.sec-summary')?.textContent).toBe('30%');
		expect(container.querySelector('.sec-credits')).toBeNull();
	});
});

describe('ContextUsageBar — live per-call breakdown', () => {
	const live = (totalTokens: number) => ({
		categories: [
			{ name: 'Messages', tokens: totalTokens, color: '#333' },
			{ name: 'Free space', tokens: 200_000 - totalTokens, color: '#444' },
		],
		totalTokens,
		maxTokens: 200_000,
		rawMaxTokens: 200_000,
		percentage: (totalTokens / 200_000) * 100,
		model: 'claude-x',
		measured: true,
		isAutoCompactEnabled: false,
	});

	it('applies a pushed snapshot with no breakdown fetch available', async () => {
		// The default stub reports unavailable, so the rendered breakdown can
		// only have come from the pushed snapshot.
		mocks.activeSession.set({
			...mocks.DEFAULT,
			sessionId: 's1',
			isStreaming: true,
			liveContextUsage: live(120_000),
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		await waitFor(() =>
			expect(container.querySelector('.ctx-pct')?.textContent).toContain('60%')
		);
	});

	it('tracks successive model calls within one turn', async () => {
		mocks.activeSession.set({
			...mocks.DEFAULT,
			sessionId: 's1',
			isStreaming: true,
			liveContextUsage: live(40_000),
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		await waitFor(() =>
			expect(container.querySelector('.ctx-pct')?.textContent).toContain('20%')
		);

		mocks.activeSession.set({
			...mocks.activeSession.current,
			liveContextUsage: live(150_000),
		});
		await waitFor(() =>
			expect(container.querySelector('.ctx-pct')?.textContent).toContain('75%')
		);
	});

	it('renders category rows from the pushed snapshot', async () => {
		mocks.activeSession.set({
			...mocks.DEFAULT,
			sessionId: 's1',
			isStreaming: true,
			liveContextUsage: live(80_000),
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		await waitFor(() => {
			const labels = Array.from(
				container.querySelectorAll('.breakdown-row:not(.model-row) .breakdown-label')
			).map((n) => n.textContent?.trim());
			expect(labels).toContain('Messages');
		});
	});

	it('a null maxTokens does not crash the pill', async () => {
		mocks.activeSession.set({
			...mocks.DEFAULT,
			sessionId: 's1',
			isStreaming: true,
			liveContextUsage: {
				...live(1_000),
				maxTokens: null,
				rawMaxTokens: null,
				percentage: null,
			},
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.usage-indicator')).not.toBeNull();
		expect(container.querySelector('.ctx-pct')?.textContent).toContain('0%');
	});

	it('the turn-end fetch overrides the pushed snapshot', async () => {
		// The persisted final is authoritative — a live snapshot is only what
		// the last model call saw. The fetch is unavailable mid-turn, so the
		// bar runs on the pushed snapshot until the turn ends.
		mocks.activeSession.set({
			...mocks.DEFAULT,
			sessionId: 's1',
			isStreaming: true,
			liveContextUsage: live(20_000),
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		await waitFor(() =>
			expect(container.querySelector('.ctx-pct')?.textContent).toContain('10%')
		);

		// Streaming → idle is what triggers the refresh fetch.
		mocks.getContextUsage.mockResolvedValue({
			available: true,
			source: 'live',
			usage: live(180_000),
		});
		mocks.activeSession.set({ ...mocks.activeSession.current, isStreaming: false });
		await waitFor(() =>
			expect(container.querySelector('.ctx-pct')?.textContent).toContain('90%')
		);
	});
});

describe('ContextUsageBar — usage source details', () => {
	const grokSource = {
		id: 'grok_subscription', label: 'SuperGrok subscription billing endpoint', scope: 'subscription',
		scope_label: 'SuperGrok subscription allowance and plan; not xAI API billing',
		measure: 'subscription_allowance' as const, measure_label: 'Subscription allowance',
		collection: 'polled' as const, support: 'unofficial_subscription' as const,
		support_label: 'Unofficial, subscription-specific endpoint', dashboard_url: 'https://grok.com/',
		unavailable: ['xAI API spend'],
	};

	it('keeps only the dashboard link below usage, preserving provider status', async () => {
		rateLimit.applySnapshot({ windows: [], available: false, providers: [
			{ provider_id: 'grok', provider_type: 'grok', provider_name: 'SuperGrok', status: 'stale',
				last_error_kind: 'unsupported', windows: [{ type: 'seven_day', utilization: 30, resets_at: null }],
				source: grokSource.label, scope: grokSource.scope_label, source_info: grokSource,
				fetched_at: 1_000, last_success_at: 1_000, last_attempt_at: 5_000 },
		] });
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.usage-source')?.textContent?.trim())
			.toBe('Compare on the provider dashboard');
		expect(container.textContent).not.toMatch(/Source:|Reports:|Last successful update:|Last attempt:|Not reported by this source:/);
		expect(container.textContent).not.toContain(grokSource.support_label);
		expect(container.textContent).not.toContain(grokSource.scope_label);
		expect(container.querySelector('.provider-status')?.textContent)
			.toBe('Not supported for this credential; polling paused');
		const link = screen.getByRole('link', { name: 'Compare on the provider dashboard' });
		expect(link.getAttribute('href')).toBe('https://grok.com/');
		expect(link.getAttribute('rel')).toContain('noopener');
	});

	it('omits the footer when no dashboard URL is available', async () => {
		rateLimit.applySnapshot({ windows: [], available: false, providers: [
			{ provider_id: 'grok', provider_type: 'grok', status: 'fresh', source_info: { ...grokSource, dashboard_url: '' },
				windows: [{ type: 'seven_day', utilization: 30, resets_at: null }],
				fetched_at: 5_000, last_success_at: 5_000, last_attempt_at: 5_000 },
		] });
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.usage-source')).toBeNull();
		expect(container.textContent).not.toContain('Last attempt');
	});

	it('keeps only the Claude dashboard link below subscription usage', async () => {
		rateLimit.applySnapshot({
			windows: [{ type: 'five_hour', utilization: 80, resets_at: future() }],
			available: true, fetched_at: 100, attempted_at: 200, last_error_kind: 'billing_access_denied',
			claude_source: { ...grokSource, id: 'claude_oauth_usage', label: 'Claude subscription usage endpoint',
				scope_label: "Active Claude account's plan usage windows; not API credit",
				dashboard_url: 'https://claude.ai/settings/usage', unavailable: [] },
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.usage-source')?.textContent?.trim())
			.toBe('Compare on the provider dashboard');
		expect(container.textContent).not.toMatch(/Source:|Unofficial|Last attempt:|Reports:|Last successful update:/);
		expect(screen.getByRole('link', { name: 'Compare on the provider dashboard' }).getAttribute('href'))
			.toBe('https://claude.ai/settings/usage');
		expect(container.textContent).not.toContain('Not reported by this source');
	});
});
