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
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ContextUsageBar from './ContextUsageBar.svelte';
import { rateLimit } from '$lib/stores/rate-limit';

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
		DEFAULT,
		activeSession: store({ ...DEFAULT }),
		getContextUsage: vi.fn(),
		// Stands in for the hydrated registry's native-Claude auth block; the
		// real one needs a live `GET /models`. Reset in beforeEach, so every
		// other test in this file sees the un-hydrated (all-null) state.
		claudeAuth: { auth_kind: null as string | null, plan_type: null as string | null },
	};
});

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
	mocks.activeSession.set({ ...mocks.DEFAULT });
	mocks.getContextUsage.mockReset();
	mocks.getContextUsage.mockResolvedValue({ available: false, reason: 'not_connected' });
	mocks.claudeAuth = { auth_kind: null, plan_type: null };
});
afterEach(() => cleanup());

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
	it('keeps stale API balances and explains unavailable billing without inventing zero', async () => {
		rateLimit.applySnapshot({ windows: [], available: false, providers: [
			{ provider_id: 'api', provider_type: 'openai', provider_name: 'OpenAI', status: 'stale', last_error_kind: 'timeout', credits: { balance_remaining: 12.34 }, fetched_at: 123 },
			{ provider_id: 'other', provider_type: 'openai', provider_name: 'Other API', status: 'unavailable', last_error_kind: 'billing_access_denied' },
		] });
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.textContent).toContain('$12.34');
		expect(container.textContent).toContain('Last known usage · Refresh timed out');
		expect(container.textContent).toContain('Billing access required');
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

	it('starts collapsed when localStorage says so (default is expanded)', async () => {
		localStorage.setItem('aihub.contextBreakdownOpen', '0');
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
	it('shows "No usage data yet" when no windows are known', async () => {
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.usage-popover')).not.toBeNull();
		expect(container.textContent).toContain('No usage data yet');
		expect(container.querySelectorAll('.popover-row').length).toBe(0);
	});

	it('never shows the "Plan usage limits" title, whether the section is empty or has rows', async () => {
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
		localStorage.setItem('aihub.usageSectionOpen:provider:codex-91', '0');
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
		localStorage.setItem('aihub.usageSectionOpen:provider:grok-2', '0');
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
		localStorage.setItem('aihub.usageSectionOpen:provider:or-credits', '0');
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
		localStorage.setItem('aihub.usageSectionOpen:provider:or-balance-collapsed', '0');
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
		localStorage.setItem('aihub.usageSectionOpen:provider:or-both', '0');
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
