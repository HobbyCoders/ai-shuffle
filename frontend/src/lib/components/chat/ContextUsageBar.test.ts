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
		modelOverride: null,
		profile: null,
		contextUsed: null as number | null,
		totalTokensIn: 0,
		totalTokensOut: 0,
		totalCacheCreationTokens: 0,
		totalCacheReadTokens: 0,
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
	return { DEFAULT, activeSession: store({ ...DEFAULT }), getContextUsage: vi.fn() };
});

vi.mock('$lib/stores/active-session', async (importOriginal) => {
	const actual = await importOriginal<Record<string, unknown>>();
	return { ...actual, activeSession: mocks.activeSession };
});
vi.mock('$lib/api/sessions', async (importOriginal) => {
	const actual = await importOriginal<Record<string, unknown>>();
	return { ...actual, getContextUsage: mocks.getContextUsage };
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
		mocks.activeSession.set({ ...mocks.DEFAULT, sessionId: 's1' });
		const { container } = render(ContextUsageBar);
		const pill = container.querySelector('.usage-indicator') as HTMLButtonElement;

		// The local model ('sonnet', default) has a 200k window — if the title fell
		// back to it, it would read "200k". It must read the breakdown's 1M instead.
		await waitFor(() => expect(pill.getAttribute('title')).toContain('1M'));
		expect(pill.getAttribute('title')).not.toContain('200k');
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

	it('collapses on toggle: hides rows but keeps the segmented bar + limits, persists to localStorage', async () => {
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		const header = container.querySelector('.ctx-header') as HTMLButtonElement;

		await fireEvent.click(header);

		// Rows hidden; the segmented bar and the plan-limits section stay visible.
		expect(container.querySelectorAll('.breakdown-row').length).toBe(0);
		expect(container.querySelector('.ctx-segbar')).not.toBeNull();
		expect(container.textContent).toContain('Plan usage limits');
		expect(header.getAttribute('aria-expanded')).toBe('false');
		expect(localStorage.getItem('aihub.contextBreakdownOpen')).toBe('0');
	});

	it('starts collapsed when localStorage says so (default is expanded)', async () => {
		localStorage.setItem('aihub.contextBreakdownOpen', '0');
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.ctx-header')?.getAttribute('aria-expanded')).toBe('false');
		expect(container.querySelectorAll('.breakdown-row').length).toBe(0);
		// The segmented bar is not part of the collapse.
		expect(container.querySelector('.ctx-segbar')).not.toBeNull();
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

	it('renders only the dim buffer segment on a fresh session', async () => {
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		const segs = container.querySelectorAll('.ctx-seg');
		expect(segs.length).toBe(1);
		expect(segs[0].classList.contains('dim')).toBe(true);
	});

	it('renders one segment per in-bar category plus a dim buffer segment, excluding Free space AND Output tokens', async () => {
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

		const segs = Array.from(container.querySelectorAll('.ctx-seg'));
		// Input + cacheRead + cacheCreation segments + 1 dim buffer segment. Output
		// tokens are session-cumulative and NOT part of totalUsed, so — unlike
		// Free space, which is the bare track — it renders no segment at all.
		expect(segs.length).toBe(4);
		expect(segs.filter((s) => s.classList.contains('dim')).length).toBe(1);
		// The rows still include both Output tokens and Free space (5 category rows
		// total, plus the trailing Model row), but neither is a segment.
		expect(container.querySelectorAll('.breakdown-row:not(.model-row)').length).toBe(5);
		// Segment widths (as fractions of contextMax) plus the buffer sum to the
		// same total the header reports — the invariant this fix restores.
		const contextMax = 200_000; // default model's context window in tests
		const segTokens = Math.round(
			segs
				.filter((s) => !s.classList.contains('dim'))
				.reduce((sum, s) => sum + (parseFloat((s as HTMLElement).style.width) / 100) * contextMax, 0)
		);
		expect(segTokens).toBe(20_000 + 50_000 + 30_000); // input + cacheRead + cacheCreation
	});

	it('renders a deferred category row with tokens + em-dash (no percent) and no bar segment', async () => {
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
		// the only non-dim segment; no dim buffer here since there's no autocompact
		// category in this fixture.
		const segs = Array.from(container.querySelectorAll('.ctx-seg'));
		expect(segs.length).toBe(1);
		expect(segs[0].classList.contains('dim')).toBe(false);
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
	it('does not render the footer when no rich breakdown is loaded (poller estimate only)', async () => {
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		expect(container.querySelector('.popover-footer')).toBeNull();
		expect(container.textContent).not.toContain('estimated from last turn');
	});

	it('renders the footer with "live breakdown" once a rich breakdown loads', async () => {
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
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		await waitFor(() => expect(container.querySelector('.popover-footer')).not.toBeNull());
		expect(container.querySelector('.footer-status')?.textContent).toBe('live breakdown');
	});

	it('renders the footer with "final breakdown" for a persisted snapshot', async () => {
		mocks.getContextUsage.mockResolvedValue({
			available: true,
			source: 'persisted',
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
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		await waitFor(() =>
			expect(container.querySelector('.footer-status')?.textContent).toBe('final breakdown')
		);
	});

	it('never shows the model label in the footer', async () => {
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
		const { container } = render(ContextUsageBar);
		await openPopover(container);

		await waitFor(() => expect(container.querySelector('.popover-footer')).not.toBeNull());
		expect(container.querySelector('.footer-model')).toBeNull();
		expect(container.querySelector('.popover-footer')?.textContent).not.toContain('Sonnet');
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

	it('lists the resolved window percentage (snapshot is 0–100)', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 42, resets_at: future() }],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		const rows = container.querySelectorAll('.popover-row');
		expect(rows.length).toBe(1);
		expect(rows[0].querySelector('.row-pct')?.textContent).toBe('42%');
	});

	it('normalises a live SDK fraction (0–1) to a percentage', async () => {
		rateLimit.applyLiveEvent({
			status: 'allowed',
			rate_limit_type: 'five_hour',
			utilization: 0.42, // SDK fraction → 42%
			resets_at: future(),
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.popover-row .row-pct')?.textContent).toBe('42%');
	});

	it('shows 0% once a window has rolled over', async () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 42, resets_at: past() }],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelector('.popover-row .row-pct')?.textContent).toBe('0%');
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
		// A lone unlabeled install keeps the flat limits list (no section header).
		expect(container.querySelector('.popover-section')).toBeNull();
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
		expect(container.textContent).toContain('Studio');
		expect(container.textContent).toContain('a@example.com');
		expect(container.textContent).not.toContain('Worker');
		expect(container.textContent).not.toContain('b@example.com');
		expect(container.textContent).not.toContain('88%');
	});

	it('heads the self section with the resolved install device + account label', async () => {
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
			],
		});
		const { container } = render(ContextUsageBar);
		await openPopover(container);
		expect(container.querySelectorAll('.popover-section').length).toBe(1);
		expect(container.querySelector('.section-device')?.textContent).toBe('Studio');
		expect(container.querySelector('.section-account')?.textContent).toBe('a@example.com');
	});

	it('unlabeled self keeps a flat limits list (no header, no other installs)', async () => {
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
		// Unlabeled self → no section header at all.
		expect(container.querySelector('.popover-section')).toBeNull();
		expect(container.textContent).not.toContain('This device');
		expect(container.textContent).not.toContain('Other device');
		// Only the self window is shown (12%), not the worker's live 50%.
		expect(container.querySelectorAll('.popover-row').length).toBe(1);
		expect(container.textContent).not.toContain('50%');
	});
});
