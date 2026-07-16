import { describe, expect, it } from 'vitest';
import { AUTO_COMPACTION_BASE } from '$lib/constants/chat';
import {
	breakdownStatusLabel,
	computeContextBreakdown,
	decideBreakdownRefresh,
	formatTokenCount,
	mapSdkContextUsage,
	resolveCategoryColor,
	usageSeverity,
	CONTEXT_CATEGORY_PALETTE,
	type BreakdownRefreshState,
	type ContextUsageInput,
} from './context-usage';
import type { SdkContextUsage } from '$lib/api/sessions';

describe('formatTokenCount', () => {
	it('renders sub-1000 counts raw', () => {
		expect(formatTokenCount(0)).toBe('0');
		expect(formatTokenCount(999)).toBe('999');
	});

	it('renders thousands with a k suffix, dropping trailing .0', () => {
		expect(formatTokenCount(1234)).toBe('1.2k');
		expect(formatTokenCount(200_000)).toBe('200k');
	});

	it('renders millions with an M suffix', () => {
		expect(formatTokenCount(1_500_000)).toBe('1.5M');
		expect(formatTokenCount(1_000_000)).toBe('1M');
	});
});

describe('usageSeverity', () => {
	it('maps percent to the same thresholds as the usage-limit pill', () => {
		expect(usageSeverity(0)).toBe('neutral');
		expect(usageSeverity(69.9)).toBe('neutral');
		expect(usageSeverity(70)).toBe('warning');
		expect(usageSeverity(90)).toBe('warning');
		expect(usageSeverity(90.1)).toBe('danger');
	});
});

describe('computeContextBreakdown', () => {
	const base: ContextUsageInput = {
		rawContextUsed: 100_000,
		inputTokens: 20_000,
		outputTokens: 5_000,
		cacheCreationTokens: 30_000,
		cacheReadTokens: 50_000,
		contextMax: 200_000,
	};

	it('totals system overhead plus conversation tokens', () => {
		const b = computeContextBreakdown(base);
		expect(b.totalUsed).toBe(AUTO_COMPACTION_BASE + 100_000);
		expect(b.contextMax).toBe(200_000);
		// (33000 + 100000) / 200000 = 66.5%
		expect(b.totalPercent).toBeCloseTo(66.5, 5);
		expect(b.severity).toBe('neutral');
	});

	it('emits one row per token type plus a trailing Free space row (no overhead row)', () => {
		const b = computeContextBreakdown(base);
		expect(b.rows.map((r) => r.key)).toEqual([
			'input',
			'cacheRead',
			'cacheCreation',
			'output',
			'freeSpace',
		]);
		// The System overhead row is gone; its tokens live on autocompactTokens.
		expect(b.rows.find((r) => r.key === 'overhead')).toBeUndefined();
		expect(b.autocompactTokens).toBe(AUTO_COMPACTION_BASE);
		const input = b.rows.find((r) => r.key === 'input')!;
		expect(input.tokens).toBe(20_000);
		expect(input.barPercent).toBeCloseTo(10, 5); // 20k / 200k
		const output = b.rows.find((r) => r.key === 'output')!;
		expect(output.subline).toBe('session total · not in context');
		// Free space = contextMax - totalUsed (which includes the buffer), grayed + last.
		const free = b.rows.find((r) => r.key === 'freeSpace')!;
		expect(free.muted).toBe(true);
		expect(free.tokens).toBe(200_000 - (AUTO_COMPACTION_BASE + 100_000));
		expect(b.rows[b.rows.length - 1].key).toBe('freeSpace');
	});

	it('flags inBar only on rows whose tokens are part of totalUsed (input/cacheRead/cacheCreation, not output or Free space)', () => {
		const b = computeContextBreakdown(base);
		expect(b.rows.find((r) => r.key === 'input')!.inBar).toBe(true);
		expect(b.rows.find((r) => r.key === 'cacheRead')!.inBar).toBe(true);
		expect(b.rows.find((r) => r.key === 'cacheCreation')!.inBar).toBe(true);
		// Output tokens are session-cumulative, NOT part of context used — never a segment.
		expect(b.rows.find((r) => r.key === 'output')!.inBar).toBe(false);
		expect(b.rows.find((r) => r.key === 'freeSpace')!.inBar).toBe(false);
	});

	it('invariant: sum of inBar row tokens plus autocompactTokens equals totalUsed (estimate path)', () => {
		const b = computeContextBreakdown({
			rawContextUsed: 987_654,
			inputTokens: 111_111,
			outputTokens: 222_222,
			cacheCreationTokens: 333_333,
			cacheReadTokens: 543_210,
			contextMax: 1_000_000,
		});
		const inBarTokens = b.rows.filter((r) => r.inBar).reduce((sum, r) => sum + r.tokens, 0);
		expect(inBarTokens + b.autocompactTokens).toBe(b.totalUsed);
	});

	it('keeps showPercent true for every row on the estimate path (no deferred concept there)', () => {
		const b = computeContextBreakdown(base);
		expect(b.rows.every((r) => r.showPercent === true)).toBe(true);
	});

	it('gives category rows fallback-palette colours and Free space a muted swatch', () => {
		const b = computeContextBreakdown(base);
		expect(b.rows.find((r) => r.key === 'input')!.color).toBe(CONTEXT_CATEGORY_PALETTE[0]);
		expect(b.rows.find((r) => r.key === 'cacheRead')!.color).toBe(CONTEXT_CATEGORY_PALETTE[1]);
		expect(b.rows.find((r) => r.key === 'cacheCreation')!.color).toBe(CONTEXT_CATEGORY_PALETTE[2]);
		expect(b.rows.find((r) => r.key === 'output')!.color).toBe(CONTEXT_CATEGORY_PALETTE[3]);
		// Free space is not a palette hue — it's the neutral muted swatch.
		expect(b.rows.find((r) => r.key === 'freeSpace')!.color).not.toBe(CONTEXT_CATEGORY_PALETTE[0]);
	});

	it('clamps the total and bars at 100% when over the window', () => {
		const b = computeContextBreakdown({
			...base,
			rawContextUsed: 5_000_000,
			cacheReadTokens: 5_000_000,
		});
		expect(b.totalPercent).toBe(100);
		expect(b.severity).toBe('danger');
		expect(b.rows.find((r) => r.key === 'cacheRead')!.barPercent).toBe(100);
	});

	it('handles the empty/zero-usage session without dividing by zero', () => {
		const b = computeContextBreakdown({
			rawContextUsed: 0,
			inputTokens: 0,
			outputTokens: 0,
			cacheCreationTokens: 0,
			cacheReadTokens: 0,
			contextMax: 200_000,
		});
		expect(b.totalUsed).toBe(AUTO_COMPACTION_BASE);
		expect(b.rows.every((r) => r.barPercent >= 0)).toBe(true);
	});

	it('marks every category empty and suppresses the Free space row on a fresh session', () => {
		const b = computeContextBreakdown({
			rawContextUsed: 0,
			inputTokens: 0,
			outputTokens: 0,
			cacheCreationTokens: 0,
			cacheReadTokens: 0,
			contextMax: 200_000,
		});
		// No Free space row while the estimate has no real data — just em-dash rows.
		expect(b.rows.map((r) => r.key)).toEqual(['input', 'cacheRead', 'cacheCreation', 'output']);
		expect(b.rows.every((r) => r.empty)).toBe(true);
		// Em-dash placeholder rows never contribute a bar segment either.
		expect(b.rows.every((r) => r.inBar === false)).toBe(true);
		expect(b.rows.find((r) => r.key === 'freeSpace')).toBeUndefined();
		// The buffer is still folded in, so the pill/header stay non-zero.
		expect(b.totalUsed).toBe(AUTO_COMPACTION_BASE);
		expect(b.autocompactTokens).toBe(AUTO_COMPACTION_BASE);
	});

	it('marks no category row empty and adds Free space once any usage has landed', () => {
		const b = computeContextBreakdown({ ...base });
		expect(b.rows.filter((r) => !r.muted).every((r) => r.empty === false)).toBe(true);
		expect(b.rows.find((r) => r.key === 'freeSpace')).toBeDefined();

		// A single cache-read token is enough to leave the empty state.
		const b2 = computeContextBreakdown({
			rawContextUsed: 0,
			inputTokens: 0,
			outputTokens: 0,
			cacheCreationTokens: 0,
			cacheReadTokens: 1,
			contextMax: 200_000,
		});
		expect(b2.rows.every((r) => r.empty === false)).toBe(true);
		expect(b2.rows.find((r) => r.key === 'freeSpace')).toBeDefined();
	});
});

describe('resolveCategoryColor', () => {
	it('passes through valid hex / rgb() / hsl() colours verbatim', () => {
		expect(resolveCategoryColor('#abc', 0)).toBe('#abc');
		expect(resolveCategoryColor('#1a2b3c', 0)).toBe('#1a2b3c');
		expect(resolveCategoryColor('#1a2b3c80', 0)).toBe('#1a2b3c80');
		expect(resolveCategoryColor('rgb(1, 2, 3)', 1)).toBe('rgb(1, 2, 3)');
		expect(resolveCategoryColor('hsl(210, 50%, 50%)', 2)).toBe('hsl(210, 50%, 50%)');
	});

	it('falls back to the palette (by wrapping index) for missing / unparseable colours', () => {
		expect(resolveCategoryColor(undefined, 0)).toBe(CONTEXT_CATEGORY_PALETTE[0]);
		expect(resolveCategoryColor(null, 1)).toBe(CONTEXT_CATEGORY_PALETTE[1]);
		expect(resolveCategoryColor('', 2)).toBe(CONTEXT_CATEGORY_PALETTE[2]);
		expect(resolveCategoryColor('CanvasText', 3)).toBe(CONTEXT_CATEGORY_PALETTE[3]);
		// Index wraps around the palette length.
		expect(resolveCategoryColor(undefined, CONTEXT_CATEGORY_PALETTE.length)).toBe(
			CONTEXT_CATEGORY_PALETTE[0]
		);
	});
});

describe('breakdownStatusLabel', () => {
	it('reads "final breakdown" for a persisted snapshot', () => {
		expect(breakdownStatusLabel('persisted', false)).toBe('final breakdown');
		// Persistence wins even if the flag says streaming (a persisted session isn't).
		expect(breakdownStatusLabel('persisted', true)).toBe('final breakdown');
	});

	it('reads "live breakdown" for a connected session at rest', () => {
		expect(breakdownStatusLabel('live', false)).toBe('live breakdown');
	});

	it('notes the turn-end refresh cadence while a live turn streams', () => {
		expect(breakdownStatusLabel('live', true)).toBe('breakdown · refreshes at turn end');
	});

	it('falls back to the estimate wording when there is no rich breakdown', () => {
		expect(breakdownStatusLabel(null, false)).toBe('estimated from last turn');
		expect(breakdownStatusLabel(null, true)).toBe('estimated from last turn');
	});
});

describe('mapSdkContextUsage', () => {
	const sdk = (over: Partial<SdkContextUsage> = {}): SdkContextUsage => ({
		categories: [
			{ name: 'System prompt', tokens: 4_000, color: '#111' },
			{ name: 'MCP tools', tokens: 20_000, color: '#222' },
			{ name: 'Messages', tokens: 76_000, color: '#333' },
			{ name: 'Free space', tokens: 100_000, color: '#444' },
		],
		totalTokens: 100_000,
		maxTokens: 200_000,
		rawMaxTokens: 200_000,
		percentage: 50,
		model: 'claude-x',
		isAutoCompactEnabled: true,
		...over,
	});

	it('maps a normal payload with non-free categories in descending token order, Free space last', () => {
		const b = mapSdkContextUsage(sdk());
		expect(b.totalUsed).toBe(100_000);
		expect(b.contextMax).toBe(200_000);
		expect(b.totalPercent).toBe(50);
		expect(b.severity).toBe('neutral');
		expect(b.autocompactTokens).toBe(0);
		// Descending by tokens (Messages 76k > MCP 20k > System 4k), Free space last.
		expect(b.rows.map((r) => r.label)).toEqual([
			'Messages',
			'MCP tools',
			'System prompt',
			'Free space',
		]);
		expect(b.rows[b.rows.length - 1].label).toBe('Free space');
		// bar relative to maxTokens: 20k / 200k = 10%
		expect(b.rows.find((r) => r.label === 'MCP tools')!.barPercent).toBeCloseTo(10, 5);
	});

	it('uses each SDK category colour verbatim and marks Free space grayed', () => {
		const b = mapSdkContextUsage(sdk());
		// '#333' is Messages' colour, passed through unchanged.
		expect(b.rows.find((r) => r.label === 'Messages')!.color).toBe('#333');
		const free = b.rows.find((r) => r.label === 'Free space')!;
		expect(free.muted).toBe(true);
		expect(b.rows.filter((r) => !r.muted).every((r) => r.muted === false)).toBe(true);
	});

	it('flags inBar on every non-free-space row, false on Free space', () => {
		const b = mapSdkContextUsage(sdk());
		expect(b.rows.filter((r) => r.label !== 'Free space').every((r) => r.inBar === true)).toBe(
			true
		);
		expect(b.rows.find((r) => r.label === 'Free space')!.inBar).toBe(false);
	});

	it('invariant: sum of inBar row tokens plus autocompactTokens equals totalUsed (rich/SDK path)', () => {
		const b = mapSdkContextUsage(
			sdk({
				categories: [
					{ name: 'System prompt', tokens: 4_567, color: '#111' },
					{ name: 'MCP tools', tokens: 20_123, color: '#222' },
					{ name: 'Messages', tokens: 76_543, color: '#333' },
					{ name: 'Autocompact buffer', tokens: 33_000, color: '#555', isDeferred: true },
					{ name: 'Free space', tokens: 65_767, color: '#444' },
				],
				totalTokens: 101_233,
				maxTokens: 200_000,
				percentage: 50,
			})
		);
		const inBarTokens = b.rows.filter((r) => r.inBar).reduce((sum, r) => sum + r.tokens, 0);
		expect(inBarTokens + b.autocompactTokens).toBe(b.totalUsed);
	});

	it('falls back to the palette when a category colour is unparseable', () => {
		const b = mapSdkContextUsage(
			sdk({
				categories: [
					{ name: 'Messages', tokens: 76_000, color: 'CanvasText' },
					{ name: 'MCP tools', tokens: 20_000, color: 'var(--foo)' },
				],
			})
		);
		// Ordered Messages (idx 0), MCP tools (idx 1) — both unparseable → palette.
		expect(b.rows[0].color).toBe(CONTEXT_CATEGORY_PALETTE[0]);
		expect(b.rows[1].color).toBe(CONTEXT_CATEGORY_PALETTE[1]);
	});

	it('drops zero-token categories but keeps Free space', () => {
		const b = mapSdkContextUsage(
			sdk({
				categories: [
					{ name: 'System prompt', tokens: 4_000, color: '#111' },
					{ name: 'Memory files', tokens: 0, color: '#222' },
					{ name: 'MCP tools', tokens: -5, color: '#333' },
					{ name: 'Free space', tokens: 0, color: '#444' },
				],
			})
		);
		expect(b.rows.map((r) => r.label)).toEqual(['System prompt', 'Free space']);
	});

	it('marks non-autocompact deferred categories with a "deferred" subline', () => {
		const b = mapSdkContextUsage(
			sdk({
				categories: [
					{ name: 'Output reserve', tokens: 8_000, color: '#111', isDeferred: true },
					{ name: 'Messages', tokens: 10_000, color: '#222' },
				],
			})
		);
		expect(b.rows.find((r) => r.label === 'Output reserve')!.subline).toBe('deferred');
		expect(b.rows.find((r) => r.label === 'Messages')!.subline).toBeNull();
	});

	it('excludes the Autocompact buffer from rows but reports its tokens', () => {
		const b = mapSdkContextUsage(
			sdk({
				categories: [
					{ name: 'Messages', tokens: 76_000, color: '#333' },
					{ name: 'Autocompact buffer', tokens: 33_000, color: '#555', isDeferred: true },
					{ name: 'Free space', tokens: 91_000, color: '#444' },
				],
				totalTokens: 76_000,
			})
		);
		expect(b.rows.map((r) => r.label)).toEqual(['Messages', 'Free space']);
		expect(b.rows.find((r) => r.label === 'Autocompact buffer')).toBeUndefined();
		expect(b.autocompactTokens).toBe(33_000);
		// Buffer still folded into the total.
		expect(b.totalUsed).toBe(76_000 + 33_000);
	});

	it('clamps percentage and bars to 0–100', () => {
		const b = mapSdkContextUsage(
			sdk({
				percentage: 140,
				totalTokens: 260_000,
				categories: [{ name: 'Messages', tokens: 260_000, color: '#111' }],
			})
		);
		expect(b.totalPercent).toBe(100);
		expect(b.severity).toBe('danger');
		expect(b.rows[0].barPercent).toBe(100);
	});

	it('guards maxTokens 0 without dividing by zero', () => {
		const b = mapSdkContextUsage(
			sdk({
				maxTokens: 0,
				percentage: 0,
				categories: [{ name: 'Messages', tokens: 10_000, color: '#111' }],
			})
		);
		expect(b.rows[0].barPercent).toBe(0);
		expect(b.totalPercent).toBe(0);
	});

	it('folds the autocompact buffer into the total and recomputes the percentage', () => {
		const b = mapSdkContextUsage(
			sdk({
				categories: [
					{ name: 'System prompt', tokens: 4_000, color: '#111' },
					{ name: 'MCP tools', tokens: 20_000, color: '#222' },
					{ name: 'Messages', tokens: 76_000, color: '#333' },
					{ name: 'Autocompact buffer', tokens: 33_000, color: '#555', isDeferred: true },
					{ name: 'Free space', tokens: 67_000, color: '#444' },
				],
				totalTokens: 127_600,
				maxTokens: 200_000,
				percentage: 64,
			})
		);
		expect(b.totalUsed).toBe(160_600);
		expect(b.totalPercent).toBeCloseTo(80.3, 5);
		expect(b.severity).toBe('warning');
		expect(b.autocompactTokens).toBe(33_000);
		// The buffer is folded into the total but never rendered as its own row.
		expect(b.rows.find((r) => r.label === 'Autocompact buffer')).toBeUndefined();
	});

	it('renders non-autocompact deferred categories grayed, em-dash, ordered after Free space, and excluded from the inBar invariant sum', () => {
		const b = mapSdkContextUsage(
			sdk({
				categories: [
					{ name: 'System prompt', tokens: 4_000, color: '#111' },
					{ name: 'Messages', tokens: 76_000, color: '#333' },
					{ name: 'MCP tools (deferred)', tokens: 40_500, color: '#222', isDeferred: true },
					{ name: 'Free space', tokens: 79_500, color: '#444' },
				],
				totalTokens: 80_000,
				maxTokens: 200_000,
				percentage: 40,
			})
		);
		// Order: non-deferred non-free (desc) -> Free space -> deferred (desc).
		expect(b.rows.map((r) => r.label)).toEqual([
			'Messages',
			'System prompt',
			'Free space',
			'MCP tools (deferred)',
		]);
		const deferredRow = b.rows.find((r) => r.label === 'MCP tools (deferred)')!;
		expect(deferredRow.inBar).toBe(false);
		expect(deferredRow.muted).toBe(true);
		expect(deferredRow.showPercent).toBe(false);
		expect(deferredRow.color).not.toBe('#222');
		expect(deferredRow.subline).toBe('deferred');
		expect(deferredRow.tokens).toBe(40_500);
		// Every non-deferred row keeps showPercent true.
		expect(b.rows.filter((r) => r.label !== 'MCP tools (deferred)').every((r) => r.showPercent)).toBe(
			true
		);
		// Invariant: sum(inBar tokens) + autocompactTokens === totalUsed, EXCLUDING
		// the deferred category's tokens (they sit outside the window accounting).
		const inBarTokens = b.rows.filter((r) => r.inBar).reduce((sum, r) => sum + r.tokens, 0);
		expect(inBarTokens + b.autocompactTokens).toBe(b.totalUsed);
		expect(inBarTokens).toBe(4_000 + 76_000);
	});

	it('drops a zero-token deferred category (same drop rule as other non-free rows)', () => {
		const b = mapSdkContextUsage(
			sdk({
				categories: [
					{ name: 'Messages', tokens: 76_000, color: '#333' },
					{ name: 'MCP tools (deferred)', tokens: 0, color: '#222', isDeferred: true },
					{ name: 'Free space', tokens: 124_000, color: '#444' },
				],
				totalTokens: 76_000,
			})
		);
		expect(b.rows.map((r) => r.label)).toEqual(['Messages', 'Free space']);
	});

	it('falls back to the raw total/percentage when no autocompact category is present', () => {
		const b = mapSdkContextUsage(
			sdk({
				categories: [{ name: 'Messages', tokens: 76_000, color: '#333' }],
				totalTokens: 76_000,
				maxTokens: 200_000,
				percentage: 38,
			})
		);
		expect(b.totalUsed).toBe(76_000);
		expect(b.totalPercent).toBeCloseTo(38, 5);

		const withoutMax = mapSdkContextUsage(
			sdk({
				categories: [{ name: 'Messages', tokens: 76_000, color: '#333' }],
				totalTokens: 76_000,
				maxTokens: 0,
				percentage: 38,
			})
		);
		expect(withoutMax.totalUsed).toBe(76_000);
		expect(withoutMax.totalPercent).toBe(38);
	});
});

describe('decideBreakdownRefresh', () => {
	const S = (over: Partial<BreakdownRefreshState> = {}): BreakdownRefreshState => ({
		open: true,
		sessionId: 'sess-1',
		isStreaming: false,
		...over,
	});

	it('fetches on open for the same session (only when nothing is loaded yet)', () => {
		const prev = S({ open: false });
		const next = S({ open: true });
		expect(decideBreakdownRefresh(prev, next)).toBe('open-fetch');
	});

	it('refreshes when a turn ends — even while the popover is closed', () => {
		expect(decideBreakdownRefresh(S({ isStreaming: true }), S({ isStreaming: false }))).toBe(
			'refresh'
		);
		// Turn-end refresh fires with the popover closed too (keeps the pill accurate).
		expect(
			decideBreakdownRefresh(
				S({ open: false, isStreaming: true }),
				S({ open: false, isStreaming: false })
			)
		).toBe('refresh');
	});

	it('clears only when there is no session (closing the popover does not clear)', () => {
		expect(decideBreakdownRefresh(S(), S({ sessionId: null }))).toBe('clear');
		// Popover merely closing over the same session is idle — the breakdown stays.
		expect(decideBreakdownRefresh(S(), S({ open: false }))).toBe('idle');
	});

	it('fetches on a session change whether the popover is open or closed', () => {
		expect(decideBreakdownRefresh(S({ sessionId: 'sess-1' }), S({ sessionId: 'sess-2' }))).toBe(
			'session-fetch'
		);
		expect(
			decideBreakdownRefresh(
				S({ open: false, sessionId: 'sess-1' }),
				S({ open: false, sessionId: 'sess-2' })
			)
		).toBe('session-fetch');
	});

	it('fetches on initial mount when a session first appears (closed popover)', () => {
		const prev: BreakdownRefreshState = { open: false, sessionId: null, isStreaming: false };
		const next = S({ open: false, sessionId: 'sess-1' });
		expect(decideBreakdownRefresh(prev, next)).toBe('session-fetch');
	});

	it('stays idle while a turn streams — the display source never flips', () => {
		// The effect only ever sees these three fields; a streaming poll tick that
		// updates token counts leaves them unchanged, so the decision is `idle`
		// (no reset, no refetch) for every tick of the turn.
		const streaming = S({ isStreaming: true });
		const actions = Array.from({ length: 5 }, () =>
			decideBreakdownRefresh(streaming, streaming)
		);
		expect(actions).toEqual(['idle', 'idle', 'idle', 'idle', 'idle']);
	});

	it('does not re-fetch or reset just because the popover stays open at idle', () => {
		expect(decideBreakdownRefresh(S(), S())).toBe('idle');
	});

	it('models mount→open→stream→turn-end: fetch on mount + turn-end, none mid-stream', () => {
		const steps: BreakdownRefreshState[] = [
			{ open: false, sessionId: null, isStreaming: false }, // initial (no session)
			S({ open: false, isStreaming: false }), // session loads (closed) -> session-fetch
			S({ open: true, isStreaming: false }), //  user opens popover     -> open-fetch
			S({ open: true, isStreaming: true }), //   turn starts streaming  -> idle
			S({ open: true, isStreaming: true }), //   poll tick              -> idle
			S({ open: true, isStreaming: false }), //  turn ends              -> refresh
			S({ open: true, isStreaming: false }), //  post-turn idle         -> idle
		];
		const actions: string[] = [];
		for (let i = 1; i < steps.length; i++) {
			actions.push(decideBreakdownRefresh(steps[i - 1], steps[i]));
		}
		expect(actions).toEqual([
			'session-fetch',
			'open-fetch',
			'idle',
			'idle',
			'refresh',
			'idle',
		]);
		expect(actions.filter((a) => a === 'clear')).toHaveLength(0);
	});
});
