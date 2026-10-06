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
		contextWindowChoice: null as string | null,
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

describe('ContextUsageBar — Fable remaining quota', () => {
	it('renders an 83%-used CLI report as 17% left in the row and collapsed summary', async () => {
		rateLimit.applyLiveEvent({
			rate_limit_type: 'seven_day_overage_included', utilization: 0.83, resets_at: future(),
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		const row = Array.from(container.querySelectorAll('.popover-row')).find(
			(el) => el.querySelector('.row-label')?.textContent === 'Fable 5 limit',
		)!;
		expect(row.querySelector('.row-pct')?.textContent).toBe('17% left');
		expect((row.querySelector('.row-fill') as HTMLElement).style.width).toBe('17%');
		await fireEvent.click(container.querySelector('[aria-label="Plan usage limits — collapse"]')!);
		expect(container.querySelector('.sec-summary')?.textContent).toBe('17% left');
	});
});

describe('ContextUsageBar — collapsed Claude row and passed resets', () => {
	function threeLimits(fableReset: string) {
		rateLimit.applySnapshot({
			available: true,
			windows: [
				{ type: 'five_hour', utilization: 90, resets_at: future(), observed_at: Date.now() },
				{ type: 'seven_day', utilization: 60, resets_at: future(), observed_at: Date.now() },
				{ type: 'seven_day_overage_included', utilization: 19, resets_at: fableReset,
					observed_at: Date.now() - 6 * 86_400_000 },
			],
		});
	}

	function rowFor(container: HTMLElement, label: string) {
		return Array.from(container.querySelectorAll('.popover-row')).find(
			(el) => el.querySelector('.row-label')?.textContent === label,
		) as HTMLElement | undefined;
	}

	it('collapsed summary is the 5-hour window, not the lowest limit; expanded keeps all three', async () => {
		threeLimits(new Date(Date.now() + 2 * 86_400_000).toISOString());
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(rowFor(container, '5-hour limit')?.querySelector('.row-pct')?.textContent).toBe('90% left');
		expect(rowFor(container, 'Weekly (all models)')?.querySelector('.row-pct')?.textContent).toBe('60% left');
		expect(rowFor(container, 'Fable 5 limit')?.querySelector('.row-pct')?.textContent).toBe('19% left');
		await fireEvent.click(container.querySelector('[aria-label="Plan usage limits — collapse"]')!);
		expect(container.querySelector('.sec-summary')?.textContent).toBe('90% left');
	});

	it('shows a Fable limit past its weekly reset as refilled, without an old age', async () => {
		threeLimits(past());
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		const fable = rowFor(container, 'Fable 5 limit')!;
		expect(fable.querySelector('.row-pct')?.textContent).toBe('100% left');
		expect((fable.querySelector('.row-fill') as HTMLElement).style.width).toBe('100%');
		expect(fable.querySelector('.row-reset-inline')?.textContent?.trim()).toBe('reset · awaiting next report');
		// No per-row age: one meta line carries the check time.
		expect(fable.textContent).not.toMatch(/reported \d|age unknown/);
		expect(container.textContent).not.toContain('19% left');
	});

	it('keeps a stale Claude reading on screen with a quiet "Checked" age', async () => {
		const twoHoursAgo = Date.now() - 2 * 3_600_000;
		rateLimit.applySnapshot({
			available: true, windows: [], self_instance_id: 'self',
			snapshots: [{ instance_id: 'self', available: true, fetched_at: twoHoursAgo, windows: [
				{ type: 'five_hour', utilization: 55, resets_at: future(), observed_at: twoHoursAgo },
			] }],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.section-device')?.textContent).toBe('Claude');
		expect(container.querySelector('.usage-meta-text')?.textContent).toBe('Checked 2h ago');
		expect(container.querySelector('.sec-header')?.textContent).not.toMatch(/reported|Checked/i);
		expect(container.querySelector('.popover-row .row-pct')?.textContent).toBe('55% left');
	});

	it('summarizes a collapsed ChatGPT subscription by its 5-hour window too', async () => {
		usageSections.set({ 'provider:codex-5h': false });
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [{
				provider_id: 'codex-5h', provider_name: 'ChatGPT', provider_type: 'chatgpt', plan_type: 'pro',
				windows: [
					{ type: 'five_hour', utilization: 40, resets_at: future() },
					{ type: 'seven_day', utilization: 90, resets_at: future() },
				],
				credits: null,
			}],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.row-pct.sec-summary')?.textContent).toBe('60% left');
	});
});

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

	it('caps a four-provider popup to the space above its trigger at 1280x800', async () => {
		const originalWidth = window.innerWidth;
		const originalHeight = window.innerHeight;
		const column = document.createElement('div');
		column.className = 'chat-view';
		document.body.append(column);
		let triggerTop = 650;
		const rectSpy = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect');
		rectSpy.mockImplementation(function (this: HTMLElement) {
			if (this === column) return DOMRect.fromRect({ x: 240, y: 52, width: 1040, height: 748 });
			if (this.querySelector(':scope > .usage-indicator')) return DOMRect.fromRect({ x: 1000, y: triggerTop, width: 32, height: 32 });
			return DOMRect.fromRect({ x: 700, width: 340, height: 1000 });
		});
		try {
			window.innerWidth = 1280;
			window.innerHeight = 800;
			rateLimit.applySnapshot({ available: true, windows: [], providers: ['one', 'two', 'three', 'four'].map(id => ({
				provider_id: id, provider_name: id, provider_type: 'openrouter', windows: [], credits: { limit_remaining: 10 },
			})) });
			const { container, unmount } = render(ContextUsageBar, { target: column });
			await openPopover(container);
			const popover = container.querySelector('.usage-popover') as HTMLElement;
			for (const name of ['one', 'two', 'three', 'four']) {
				expect(container.querySelector(`[aria-label="${name} usage — collapse"]`)).not.toBeNull();
			}
			// Trigger top minus column top, 12px inset and the 6px opening gap.
			await waitFor(() => expect(popover.style.getPropertyValue('--usage-popover-height')).toBe(`${650 - 52 - 12 - 6}px`));
			triggerTop = 450;
			await fireEvent(window, new Event('resize'));
			await waitFor(() => expect(popover.style.getPropertyValue('--usage-popover-height')).toBe(`${450 - 52 - 12 - 6}px`));
			await unmount();
		} finally {
			rectSpy.mockRestore();
			window.innerWidth = originalWidth;
			window.innerHeight = originalHeight;
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

describe('ContextUsageBar — the chat\'s picked context window', () => {
	const report = (totalTokens: number, maxTokens: number) => ({
		categories: [
			{ name: 'Messages', tokens: totalTokens, color: '#333' },
			{ name: 'Free space', tokens: Math.max(maxTokens - totalTokens, 0), color: '#444' },
		],
		totalTokens,
		maxTokens,
		rawMaxTokens: maxTokens,
		percentage: (totalTokens / maxTokens) * 100,
		model: 'claude-x',
		isAutoCompactEnabled: true,
	});
	const persisted = (totalTokens: number, maxTokens: number) => ({
		available: true, source: 'persisted', captured_at: new Date().toISOString(), usage: report(totalTokens, maxTokens),
	});
	const pillOf = (container: HTMLElement) => container.querySelector('.usage-indicator') as HTMLButtonElement;
	const headerValue = (container: HTMLElement) =>
		container.querySelector('.ctx-value')?.textContent?.replace(/\s+/g, ' ').trim();

	it('shows a saved 200k window on a 1M-capable model as 200k everywhere', async () => {
		mocks.activeSession.set({ ...mocks.DEFAULT, sessionId: 's1', modelOverride: 'fable', contextWindowChoice: 'standard' });
		const { container } = render(ContextUsageBar);
		expect(pillOf(container).getAttribute('title')).toContain('0 / 200k tokens');
		await openPopover(container);
		expect(headerValue(container)).toBe('0 / 200k (0%)');
		expect(container.textContent).not.toContain('1M');
	});

	it('shows the model capacity when no window is saved', async () => {
		mocks.activeSession.set({ ...mocks.DEFAULT, sessionId: 's1', modelOverride: 'fable' });
		const { container } = render(ContextUsageBar);
		expect(pillOf(container).getAttribute('title')).toContain('0 / 1M tokens');
	});

	it('caps a 1M preference at what the model can hold', async () => {
		mocks.activeSession.set({ ...mocks.DEFAULT, sessionId: 's1', modelOverride: 'haiku', contextWindowChoice: '1m' });
		const { container } = render(ContextUsageBar);
		expect(pillOf(container).getAttribute('title')).toContain('0 / 200k tokens');
	});

	it('shows a new chat the window its first turn will use', async () => {
		mocks.activeSession.set({ ...mocks.DEFAULT, modelOverride: 'fable', contextWindowChoice: 'standard' });
		const { container } = render(ContextUsageBar);
		expect(pillOf(container).getAttribute('title')).toContain('0 / 200k tokens');
		mocks.activeSession.set({ ...mocks.activeSession.current, contextWindowChoice: '1m' });
		await waitFor(() => expect(pillOf(container).getAttribute('title')).toContain('0 / 1M tokens'));
	});

	it('moves to an acknowledged pick at once, re-measuring the last turn\'s numbers', async () => {
		mocks.getContextUsage.mockResolvedValue(persisted(100_000, 1_000_000));
		mocks.activeSession.set({ ...mocks.DEFAULT, sessionId: 's1', modelOverride: 'fable', contextWindowChoice: '1m' });
		const { container } = render(ContextUsageBar);
		await waitFor(() => expect(pillOf(container).getAttribute('title')).toContain('100k / 1M tokens'));
		expect(pillOf(container).getAttribute('aria-label')).toMatch(/10% of context window used/);

		// What activeSession.setContextWindowChoice publishes once the PUT is acknowledged.
		mocks.activeSession.set({ ...mocks.activeSession.current, contextWindowChoice: 'standard' });
		await waitFor(() => expect(pillOf(container).getAttribute('title')).toContain('100k / 200k tokens'));
		expect(pillOf(container).getAttribute('aria-label')).toMatch(/50% of context window used/);
		await openPopover(container);
		expect(headerValue(container)).toBe('100k / 200k (50%)');
		const free = Array.from(container.querySelectorAll('.breakdown-row')).find((row) =>
			row.querySelector('.breakdown-label')?.textContent?.trim() === 'Free space');
		expect(free?.querySelector('.breakdown-value')?.textContent).toContain('100k');
		expect(mocks.getContextUsage).toHaveBeenCalledTimes(1);
	});

	it('honours the saved window restored on reload over an older 1M report', async () => {
		// After a full reload the switcher hydrates the choice from /live metadata;
		// the persisted breakdown still carries the previous turn's window.
		mocks.getContextUsage.mockResolvedValue(persisted(40_000, 1_000_000));
		mocks.activeSession.set({ ...mocks.DEFAULT, sessionId: 's1', modelOverride: 'fable', contextWindowChoice: 'standard' });
		const { container } = render(ContextUsageBar);
		await waitFor(() => expect(pillOf(container).getAttribute('title')).toContain('40k / 200k tokens'));
		expect(pillOf(container).getAttribute('aria-label')).toMatch(/20% of context window used/);
	});

	it('carries nothing over when switching to another chat', async () => {
		mocks.getContextUsage.mockImplementation(async (sid: string) =>
			sid === 'chat-a' ? persisted(150_000, 1_000_000) : { available: false, reason: 'not_connected' });
		mocks.activeSession.set({ ...mocks.DEFAULT, sessionId: 'chat-a', modelOverride: 'fable', contextWindowChoice: 'standard' });
		const { container } = render(ContextUsageBar);
		await waitFor(() => expect(pillOf(container).getAttribute('title')).toContain('150k / 200k tokens'));

		mocks.activeSession.set({ ...mocks.DEFAULT, sessionId: 'chat-b', modelOverride: 'fable', contextWindowChoice: '1m' });
		await waitFor(() => expect(pillOf(container).getAttribute('title')).toContain('0 / 1M tokens'));
		expect(pillOf(container).getAttribute('title')).not.toContain('150k');

		mocks.activeSession.set({ ...mocks.DEFAULT, sessionId: 'chat-a', modelOverride: 'fable', contextWindowChoice: 'standard' });
		await waitFor(() => expect(pillOf(container).getAttribute('title')).toContain('150k / 200k tokens'));
	});

	it('keeps a running turn on its own window and says when the pick takes over', async () => {
		mocks.activeSession.set({
			...mocks.DEFAULT, sessionId: 's1', modelOverride: 'fable', contextWindowChoice: 'standard',
			isStreaming: true, liveContextUsage: report(300_000, 1_000_000),
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		await waitFor(() => expect(headerValue(container)).toBe('300k / 1M (30%)'));
		expect(container.querySelector('.ctx-next')?.textContent).toBe('This turn uses 1M · 200k from the next turn');
		expect(pillOf(container).getAttribute('title')).toContain('200k from the next turn');

		// The turn ends: the next one runs at 200k, which 300k no longer fits.
		mocks.activeSession.set({ ...mocks.activeSession.current, isStreaming: false });
		await waitFor(() => expect(headerValue(container)).toBe('300k / 200k (over limit)'));
		expect(container.querySelector('.ctx-next')).toBeNull();
		expect(container.querySelector('.ctx-pct')?.classList.contains('danger')).toBe(true);
		expect(container.querySelector('svg.usage-ring')?.classList.contains('danger')).toBe(true);
		expect(pillOf(container).getAttribute('aria-label')).toBe('Context and usage — over the limit, 300k / 200k tokens');
	});

	it('does not treat the previous turn\'s report as the running turn\'s window', async () => {
		mocks.getContextUsage.mockResolvedValue(persisted(50_000, 1_000_000));
		mocks.activeSession.set({ ...mocks.DEFAULT, sessionId: 's1', modelOverride: 'fable', contextWindowChoice: 'standard' });
		const { container } = render(ContextUsageBar);
		await waitFor(() => expect(pillOf(container).getAttribute('title')).toContain('50k / 200k tokens'));
		// A turn starts after the pick; no per-call snapshot yet.
		mocks.activeSession.set({ ...mocks.activeSession.current, isStreaming: true });
		await openPopover(container);
		expect(headerValue(container)).toBe('50k / 200k (25%)');
		expect(container.querySelector('.ctx-next')).toBeNull();
	});

	it('prints "Over" under the phone ring when past the limit', async () => {
		mocks.getContextUsage.mockResolvedValue(persisted(300_000, 1_000_000));
		mocks.activeSession.set({ ...mocks.DEFAULT, sessionId: 's1', modelOverride: 'fable', contextWindowChoice: 'standard' });
		const { container } = render(ContextUsageBar, { props: { showPercent: true } });
		await waitFor(() => expect(container.querySelector('.usage-percent')?.textContent).toBe('Over'));
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
		// No guidance to act on: the credits line says what is known, and the
		// meta line only says when it was checked.
		expect(container.querySelector('.provider-status')).toBeNull();
		expect(container.querySelector('.usage-credits')?.textContent).toBe(cached ? '$12.34 left' : 'Credit balance not available for this key');
		expect(container.querySelector('.usage-meta-text')?.textContent).toMatch(/^(Checked |Not checked yet$)/);
		expect(container.textContent).not.toMatch(/Last known|Last reported|Refresh pending|Refresh timed out|Usage unavailable/);
		expect(container.querySelector('a[href*="platform.openai.com/settings/organization/billing"]')).toBeNull();
		if (cached) {
			expect(container.textContent).toContain('$12.34 left');
			expect(container.textContent).toContain('42%');
			expect(container.textContent).toMatch(/resets in/);
		} else {
			expect(container.textContent).not.toContain('Claude');
			expect(container.textContent).not.toContain('$0.00');
		}
		await fireEvent.click(getByRole('button', { name: 'OpenAI usage — collapse' }));
		expect(container.querySelector('.sec-summary')?.textContent).toBe(cached ? '$12.34 left' : 'Balance unavailable');
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


	it.each([true, false])('keeps ChatGPT extra credits in the expanded rows only (windows: %s)', async (withWindows) => {
		rateLimit.applySnapshot({ windows: [], available: false, providers: [
			{ provider_id: 'codex', provider_type: 'chatgpt', provider_name: 'ChatGPT', status: 'fresh', fetched_at: Date.now(),
				credits: { balance: 12.5 },
				windows: withWindows ? [{ type: 'five_hour', utilization: 30, resets_at: future() }] : [] },
		] });
		const { container, getByRole } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.usage-credits')?.textContent).toBe('12.5 extra credits available');
		await fireEvent.click(getByRole('button', { name: 'OpenAI usage — collapse' }));
		const header = getByRole('button', { name: 'OpenAI usage — expand' });
		expect(header.textContent).not.toContain('extra credits');
		if (withWindows) expect(header.textContent).toContain('70% left');
		// Nothing to summarize yet: the summary slot holds a dash, not a credit count.
		else expect(container.querySelector('.sec-summary')?.textContent).toBe('—');
		expect(container.textContent).not.toContain('extra credits');
	});

	it('labels official organization costs as spend rather than credit remaining', async () => {
		rateLimit.applySnapshot({ windows: [], available: true, providers: [
			{ provider_id: 'admin-api', provider_type: 'openai', provider_name: 'OpenAI', status: 'fresh',
				credits: { spend_month: 12.34, input_tokens: 20, output_tokens: 30, num_model_requests: 2 } },
		] });
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.textContent).toContain('$12.34 organization spend this month (UTC); balance unavailable');
		expect(container.textContent).not.toContain('$12.34 left');
	});

	it.each(['unavailable', 'stale'] as const)('says the balance is not available, without a billing link, when OpenAI billing is %s', async (status) => {
		rateLimit.applySnapshot({ windows: [], available: false, providers: [
			{ provider_id: 'api', provider_type: 'openai', provider_name: 'OpenAI', status,
				last_error_kind: 'billing_access_denied',
				...(status === 'stale' ? { credits: { balance_remaining: 12.34 }, fetched_at: 123 } : {}) },
		] });
		const { container, getByRole, queryByRole } = render(ContextUsageBar);
		await openPopover(container);
		// The action rides the one meta line; the credits line says what is known.
		// No billing access can expose an API key's prepaid balance, so a key
		// without one is not asked for it.
		expect(container.querySelector('.provider-status')?.textContent ?? null).toBe(status === 'stale' ? 'Billing access required' : null);
		expect(container.querySelector('.usage-credits')?.textContent).toBe(status === 'stale' ? '$12.34 left' : 'Credit balance not available for this key');
		expect(container.textContent).not.toContain('Last known usage');
		expect(container.textContent).not.toContain('Sign in');
		expect(container.textContent).not.toContain('$0.00');
		if (status === 'stale') {
			expect(container.textContent).toContain('$12.34 left');
		}
		expect(queryByRole('link', { name: /View balance/ })).toBeNull();
		await fireEvent.click(getByRole('button', { name: 'OpenAI usage — collapse' }));
		expect(queryByRole('link', { name: /View balance/ })).toBeNull();
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
		expect(container.querySelector('a[href*="platform.openai.com/settings/organization/billing"]')).toBeNull();
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
		expect(container.textContent).toContain('Credit balance not available for this key');
		expect(container.textContent).not.toContain('Sign in');
		expect(container.textContent).toContain('Balance unavailable for this key');
		expect(container.querySelector('a[href*="platform.openai.com/settings/organization/billing"]')).toBeNull();
	});

	it('does not ask for billing access when fal.ai refuses the ordinary key', async () => {
		rateLimit.applySnapshot({ windows: [], available: false, providers: [
			{ provider_id: 'fal-ai', provider_type: 'fal', provider_name: 'fal.ai', status: 'unavailable', last_error_kind: 'billing_access_denied' },
		] });
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.usage-credits')?.textContent).toBe('Balance unavailable for this key');
		expect(container.querySelector('.provider-status')).toBeNull();
		expect(container.textContent).not.toContain('Billing access required');
		expect(container.textContent).not.toContain('$0.00');
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
		expect(container.textContent).toContain('Credit balance not available for this key');
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
		// fill; the plan-limits section (its rows, placeholders before a report)
		// stays visible even though the "Plan usage limits" title is gone.
		expect(container.querySelectorAll('.breakdown-row').length).toBe(0);
		const segs = container.querySelectorAll('.ctx-seg');
		expect(segs.length).toBe(1);
		expect(segs[0].classList.contains('solid')).toBe(true);
		expect(container.textContent).not.toContain('Plan usage limits');
		expect(container.textContent).toContain('5-hour limit');
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

	it.each(['api', 'subscription'])('says nothing is reported yet for %s credentials before a usage snapshot arrives', async (authKind) => {
		mocks.claudeAuth = { auth_kind: authKind, plan_type: null };
		mocks.claudeAuthenticated = authKind === 'subscription';
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.usage-popover')).not.toBeNull();
		expect(container.querySelector('.usage-meta-text')?.textContent).toBe('Not reported yet');
		// A plan login holds its 5-hour and weekly rows for the first report; an
		// API-key login reports no windows, so it has none to hold.
		const placeholders = Array.from(container.querySelectorAll('.popover-row.placeholder'), (row) => row.querySelector('.row-label')?.textContent);
		expect(placeholders).toEqual(authKind === 'api' ? [] : ['5-hour limit', 'Weekly (all models)']);
		expect(container.querySelectorAll('.popover-row:not(.placeholder)').length).toBe(0);
	});

	it('never shows the "Plan usage limits" title, whether the section is empty or has rows', async () => {
		rateLimit.applySnapshot({ available: true, windows: [] });
		const { container: empty } = render(ContextUsageBar);
		await openPopover(empty);
		expect(empty.textContent).not.toContain('Plan usage limits');
		expect(empty.querySelectorAll('.popover-row.placeholder').length).toBe(2);
		cleanup();

		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 42, resets_at: future() }],
		});
		const { container: withRows } = render(ContextUsageBar);
		await openPopover(withRows);
		expect(withRows.textContent).not.toContain('Plan usage limits');
		// The reported window fills its row; the weekly one still holds its place.
		expect(withRows.querySelectorAll('.popover-row').length).toBe(2);
		expect(withRows.querySelectorAll('.popover-row:not(.placeholder)').length).toBe(1);
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

	it('shows a Claude window past its reset as refilled while it awaits another report', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 42, resets_at: past(), observed_at: Date.now() }],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.popover-row .row-pct')?.textContent).toBe('100% left');
		expect(container.textContent).not.toContain('resets in now');
		expect(container.querySelector('.popover-row .row-reset-inline')?.textContent?.trim()).toBe(
			'reset · awaiting next report'
		);
	});

	it('says when a stale Claude reading was checked in the meta line only, without "not live"', async () => {
		rateLimit.applySnapshot({
			available: true, windows: [], self_instance_id: 'self',
			snapshots: [{ instance_id: 'self', available: true, fetched_at: Date.now() - 2 * 86_400_000,
				windows: [{ type: 'five_hour', utilization: 42, resets_at: future(), observed_at: Date.now() - 2 * 86_400_000 }] }],
		});
		const { container, getByRole } = render(ContextUsageBar);
		await openPopover(container);
		const header = getByRole('button', { name: /Plan usage limits/ });
		expect(header.textContent).not.toMatch(/reported|Checked/i);
		expect(container.querySelector('.usage-meta-text')?.textContent).toBe('Checked 2d ago');
		expect(container.textContent).not.toMatch(/not live|Last reported/);
		// The figure stays the last report's.
		expect(container.querySelector('.popover-row .row-pct')?.textContent).toBe('42% left');
		// Collapsed, the header shows the figure, not the age.
		await fireEvent.click(header);
		expect(container.querySelector('.sec-summary')?.textContent).toBe('42% left');
		expect(header.textContent).not.toMatch(/reported|Checked/i);
	});

	it('keeps the as-of line when a live event refreshes only one window', async () => {
		const twoDaysAgo = Date.now() - 2 * 86_400_000;
		rateLimit.applySnapshot({
			available: true, windows: [], self_instance_id: 'self',
			snapshots: [{ instance_id: 'self', available: true, fetched_at: twoDaysAgo,
				windows: [
					{ type: 'five_hour', utilization: 42, resets_at: future(), observed_at: twoDaysAgo },
					{ type: 'seven_day', utilization: 60, resets_at: future(), observed_at: twoDaysAgo },
				] }],
		});
		rateLimit.applyLiveEvent({ rate_limit_type: 'five_hour', utilization: 0.5, resets_at: future(), instance_id: 'self' });
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		// The weekly window is still two days old.
		expect(container.querySelector('.usage-meta-text')?.textContent).toBe('Checked 2d ago');
	});

	it('says the check time is unknown for an undated Claude window instead of borrowing a newer report (#1661)', async () => {
		const now = Date.now();
		rateLimit.applySnapshot({
			available: true, windows: [], self_instance_id: 'self',
			snapshots: [{ instance_id: 'self', available: true, fetched_at: now,
				windows: [
					{ type: 'five_hour', utilization: 42, resets_at: future(), observed_at: now },
					// Persisted before windows were dated: no observed_at.
					{ type: 'seven_day_overage_included', utilization: 64, resets_at: future() },
				] }],
		});
		const { container, getByRole } = render(ContextUsageBar);
		await openPopover(container);
		const header = getByRole('button', { name: /Plan usage limits/ });
		expect(header.textContent).not.toMatch(/unknown|Checked/);
		expect(container.querySelector('.usage-meta-text')?.textContent).toBe('Check time unknown');
		const rows = [...container.querySelectorAll('.popover-row')];
		const fable = rows.find((row) => row.textContent?.includes('Fable 5 limit'))!;
		// The value is shown as reported, not inverted.
		expect(fable.querySelector('.row-pct')?.textContent).toBe('64% left');
	});

	it('keeps a stale provider reading on screen with a quiet "Checked" age and no countdown', async () => {
		const fetched = Date.now() - 10 * 60_000;
		rateLimit.applySnapshot({ windows: [], available: false, providers: [
			{ provider_id: 'or', provider_type: 'openrouter', provider_name: 'Router', status: 'stale',
				last_error_kind: 'unavailable', credits: { usage: 2, limit: 10 }, fetched_at: fetched,
				cooldown_until: Date.now() + 125_000 },
		] });
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		// Same freshness line as Claude: the age once, in the meta line.
		const header = container.querySelector('[aria-label^="Router usage"]') as HTMLElement;
		expect(header.textContent).not.toMatch(/reported|Checked/i);
		expect(container.querySelector('.usage-meta-text')?.textContent).toBe('Checked 10m ago');
		expect(container.textContent).not.toMatch(/not live|Updated|Next check|Last reported/);
		expect(container.querySelector('.usage-credits')?.textContent).toBe('Balance unavailable for this key');
		// Collapsed, the header shows the figure, never the age.
		await fireEvent.click(header);
		expect(header.textContent).not.toMatch(/reported|Checked/i);
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
					available: true,
					windows: [{ type: 'five_hour', utilization: 12, resets_at: future() }],
				},
				{
					instance_id: 'worker',
					device_name: 'Worker',
					available: true,
					windows: [{ type: 'five_hour', utilization: 88, resets_at: future() }],
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		// A single section + reported row for the resolved (self) install only.
		expect(container.querySelectorAll('.popover-section').length).toBe(1);
		expect(container.querySelectorAll('.popover-row:not(.placeholder)').length).toBe(1);
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
					available: true,
					windows: [{ type: 'five_hour', utilization: 12, resets_at: future(), observed_at: Date.now() }],
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
					windows: [{ type: 'five_hour', utilization: 12, resets_at: future(), observed_at: Date.now() }],
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
		expect(container.querySelectorAll('.popover-row:not(.placeholder)').length).toBe(1);
		expect(container.textContent).not.toContain('50%');
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
					windows: [{ type: 'five_hour', utilization: 12, resets_at: future(), observed_at: Date.now() }],
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.section-device')?.textContent).toBe('Claude');
		// "API" beats the device-name fallback: it says HOW this install bills.
		expect(container.querySelector('.section-account')?.textContent).toBe('API');
	});

	it("subtexts the self Claude header with the plan the CLI reports", async () => {
		mocks.claudeAuth = { auth_kind: 'subscription', plan_type: 'pro' };
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [
				{
					instance_id: 'primary',
					available: true,
					windows: [{ type: 'five_hour', utilization: 12, resets_at: future(), observed_at: Date.now() }],
				},
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.section-account')?.textContent).toBe('PRO');
	});

	it('collapses the Claude plan section on header click: hides window rows and persists', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 42, resets_at: future() }],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		// Open by default → the Claude header, its reported 5-hour row and the
		// weekly row held for its first report.
		expect(container.querySelector('.section-device')?.textContent).toBe('Claude');
		expect(container.querySelectorAll('.popover-row').length).toBe(2);

		const secHeader = container.querySelector('.sec-header') as HTMLButtonElement;
		await fireEvent.click(secHeader); // collapse
		expect(container.querySelectorAll('.popover-row').length).toBe(0);
		expect(secHeader.getAttribute('aria-expanded')).toBe('false');
		expect(localStorage.getItem('aihub.usageSectionOpen:plan')).toBe('0');
		// Collapsed → the least remaining plan pct rides on the header summary.
		expect(container.querySelector('.sec-summary')?.textContent).toBe('42% left');

		await fireEvent.click(secHeader); // expand again
		expect(container.querySelectorAll('.popover-row').length).toBe(2);
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

		// The Claude header always renders (self, empty windows → just "Claude").
		// Both are subscriptions, so the Settings order applies: OpenAI first.
		expect(container.querySelectorAll('.popover-section').length).toBe(2);
		// A ChatGPT-subscription row displays under the OpenAI brand.
		const deviceLabels = Array.from(container.querySelectorAll('.section-device')).map(
			(e) => e.textContent
		);
		expect(deviceLabels).toEqual(['OpenAI', 'Claude']);
		// The only .section-account belongs to the provider header (Claude self is unlabeled).
		expect(container.querySelector('.section-account')?.textContent).toBe('PROLITE');

		// One row per provider window (the empty Claude section's placeholder
		// rows are not counted here).
		const rows = container.querySelectorAll('.popover-row:not(.placeholder)');
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
		const accounts = Array.from(container.querySelectorAll('.section-account:not(.provider-status)')).map(
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
		// Both are subscriptions: Grok comes before Claude in the Settings order.
		expect(deviceLabels).toEqual(['Grok', 'Claude']);
		expect(container.querySelector('.section-account')?.textContent).toBe('SuperGrok');

		// SuperGrok billing reports used-%, converted to "% left" for display.
		const rows = container.querySelectorAll('.popover-row:not(.placeholder)');
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
		// Subscriptions (ChatGPT, then the Claude plan) above the API key.
		expect(deviceLabels).toEqual(['OpenAI', 'Claude', 'OpenAI']);
		const accounts = Array.from(container.querySelectorAll('.section-account:not(.provider-status)')).map(
			(e) => e.textContent
		);
		expect(accounts).toEqual(['PROLITE', 'API']);
		expect(container.querySelectorAll('.popover-section').length).toBe(3);
	});

	it('shows one remaining account balance instead of a duplicate key-spend row', async () => {
        rateLimit.applySnapshot({ available: false, windows: [], providers: [
            { provider_id: 'or-key', provider_type: 'openrouter', provider_name: 'OpenRouter',
              credits: { usage: 3, limit: 10 }, source: 'OpenRouter key API', scope: 'This model key spend', fetched_at: 1000 },
            { provider_id: 'openrouter-account:or-key', provider_type: 'openrouter_account', provider_name: 'OpenRouter',
              credits: { balance_remaining: 20.75 }, source: 'OpenRouter management API', scope: 'Account credit ledger (USD)', fetched_at: 2000 }
        ] });
        const { container } = render(ContextUsageBar);
        await openPopover(container);
        // Key and account rows share the provider's name; only the balance row shows.
        expect(screen.getAllByRole('button', { name: 'OpenRouter usage — collapse' })).toHaveLength(1);
        expect(container.textContent).not.toContain('Source:');
        expect(container.textContent).not.toContain('Account credit ledger (USD)');
        expect(container.textContent).not.toContain('Last successful update:');
        expect(container.textContent).toContain('$20.75 left');
        expect(container.textContent).not.toContain('$3.00 used');
    });

	it('sorts subscriptions above API accounts, each in the Settings > Providers order, Claude included (#1845)', async () => {
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
		const accounts = Array.from(container.querySelectorAll('.section-account:not(.provider-status)')).map(
			(e) => e.textContent
		);
		// Subscriptions in the Settings order (OpenAI, Grok, Claude), then API
		// accounts in the same order (OpenAI, OpenRouter, then fal.ai). Claude is
		// ordered by the rule like any other provider, not pinned first.
		expect(labels).toEqual(['OpenAI', 'Grok', 'Claude', 'OpenAI', 'OpenRouter', 'fal.ai']);
		expect(accounts).toEqual(['PROLITE', 'SuperGrok', 'API', 'API', 'API']);
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
		// Empty windows → no provider window rows and no bars; the only rows are
		// the empty Claude section's placeholders.
		expect(container.querySelectorAll('.popover-row:not(.placeholder)').length).toBe(0);
		expect(container.querySelectorAll('.row-bar').length).toBe(container.querySelectorAll('.popover-row.placeholder').length);
		// The credits line renders from usage / limit_remaining.
		expect(container.textContent).toContain('$6.90 left on this key');
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
		expect(container.querySelectorAll('.popover-row:not(.placeholder)').length).toBe(1);
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
		expect(chip?.textContent).toBe('$87.66 left on this key');
		// It is a header summary, not a severity pct chip.
		expect(container.querySelector('.row-pct.sec-summary')).toBeNull();
		// Collapsed → the full body credits line is hidden.
		expect(container.querySelector('.usage-credits')).toBeNull();
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
		expect(container.textContent).toContain('$87.66 left on this key');
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
		expect(container.textContent).toContain('$20.75 left');
		expect(container.textContent).not.toContain('$6.90 left on this key');
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

		expect(container.textContent).toContain('$6.90 left on this key');
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
		expect(container.querySelector('.row-pct.sec-summary')?.textContent).toBe('70% left');
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
		// The link shares the section's one meta line with the guidance.
		expect(container.querySelector('.usage-meta .provider-dashboard-link')?.textContent?.trim()).toBe('Dashboard');
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
		// The meta line stays (it says when usage was checked); only the link goes.
		expect(container.querySelector('.usage-meta')).not.toBeNull();
		expect(container.querySelector('.usage-meta a')).toBeNull();
		expect(container.textContent).not.toContain('Last attempt');
	});

	it('keeps only the Claude dashboard link below subscription usage', async () => {
		rateLimit.applySnapshot({
			windows: [{ type: 'five_hour', utilization: 80, resets_at: future() }],
			available: true, fetched_at: 100, attempted_at: null, last_error_kind: null,
			claude_source: { ...grokSource, id: 'claude_rate_limit_events', label: 'Claude Code rate-limit events',
				scope_label: 'Single plan window refreshed from the running session', collection: 'passive',
				support: 'response_metadata', support_label: 'Metadata on inference responses',
				dashboard_url: 'https://claude.ai/settings/usage', unavailable: [] },
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.textContent).not.toMatch(/Source:|Reports:|Last successful update:/);
		expect(container.textContent).not.toContain('Unofficial');
		expect(container.textContent).not.toContain('Last attempt');
		expect(screen.getByRole('link', { name: 'See all usage on claude.ai' }).getAttribute('href'))
			.toBe('https://claude.ai/settings/usage');
		// One link to the usage page, not a second "compare" link for Claude.
		expect(screen.queryByRole('link', { name: 'Compare on the provider dashboard' })).toBeNull();
	});

	it('links the claude.ai usage page even before the CLI has reported usage', async () => {
		mocks.claudeAuth = { auth_kind: 'subscription', plan_type: null };
		mocks.claudeAuthenticated = true;
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.usage-meta-text')?.textContent).toBe('Not reported yet');
		expect(screen.getByRole('link', { name: 'See all usage on claude.ai' }).getAttribute('href'))
			.toBe('https://claude.ai/settings/usage');
	});
});
