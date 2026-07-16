import { describe, expect, it } from 'vitest';
import { AUTO_COMPACTION_BASE } from '$lib/constants/chat';
import {
	breakdownStatusLabel,
	computeContextBreakdown,
	decideBreakdownRefresh,
	formatTokenCount,
	mapSdkContextUsage,
	usageSeverity,
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

	it('emits one row per token type with bars relative to contextMax', () => {
		const b = computeContextBreakdown(base);
		expect(b.rows.map((r) => r.key)).toEqual([
			'input',
			'cacheRead',
			'cacheCreation',
			'output',
			'overhead',
		]);
		const input = b.rows.find((r) => r.key === 'input')!;
		expect(input.tokens).toBe(20_000);
		expect(input.barPercent).toBeCloseTo(10, 5); // 20k / 200k
		const overhead = b.rows.find((r) => r.key === 'overhead')!;
		expect(overhead.tokens).toBe(AUTO_COMPACTION_BASE);
		expect(overhead.subline).toBe('estimated');
		const output = b.rows.find((r) => r.key === 'output')!;
		expect(output.subline).toBe('session total · not in context');
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

	it('marks every category empty on a fresh session EXCEPT the System overhead reserve', () => {
		const b = computeContextBreakdown({
			rawContextUsed: 0,
			inputTokens: 0,
			outputTokens: 0,
			cacheCreationTokens: 0,
			cacheReadTokens: 0,
			contextMax: 200_000,
		});
		const empties = b.rows.filter((r) => r.empty).map((r) => r.key);
		expect(empties).toEqual(['input', 'cacheRead', 'cacheCreation', 'output']);
		const overhead = b.rows.find((r) => r.key === 'overhead')!;
		expect(overhead.empty).toBe(false);
		expect(overhead.tokens).toBe(AUTO_COMPACTION_BASE);
		// The header total is still the reserve, so the pill/header stay non-zero.
		expect(b.totalUsed).toBe(AUTO_COMPACTION_BASE);
	});

	it('marks no row empty once any usage has landed', () => {
		const b = computeContextBreakdown({ ...base });
		expect(b.rows.every((r) => r.empty === false)).toBe(true);

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

	it('maps a normal payload to the total + one row per category', () => {
		const b = mapSdkContextUsage(sdk());
		expect(b.totalUsed).toBe(100_000);
		expect(b.contextMax).toBe(200_000);
		expect(b.totalPercent).toBe(50);
		expect(b.severity).toBe('neutral');
		expect(b.rows.map((r) => r.label)).toEqual([
			'System prompt',
			'MCP tools',
			'Messages',
			'Free space',
		]);
		// bar relative to maxTokens: 20k / 200k = 10%
		expect(b.rows.find((r) => r.label === 'MCP tools')!.barPercent).toBeCloseTo(10, 5);
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

	it('marks deferred categories with a "deferred" subline', () => {
		const b = mapSdkContextUsage(
			sdk({
				categories: [
					{ name: 'Autocompact buffer', tokens: 8_000, color: '#111', isDeferred: true },
					{ name: 'Messages', tokens: 10_000, color: '#222' },
				],
			})
		);
		expect(b.rows.find((r) => r.label === 'Autocompact buffer')!.subline).toBe('deferred');
		expect(b.rows.find((r) => r.label === 'Messages')!.subline).toBeNull();
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
		expect(b.rows.find((r) => r.label === 'Autocompact buffer')).toBeDefined();
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

	it('fetches on open (single source is fetched when the popover opens)', () => {
		const prev = S({ open: false });
		const next = S({ open: true });
		expect(decideBreakdownRefresh(prev, next)).toBe('open-fetch');
	});

	it('refreshes when a turn ends (fetched outside streaming, not only during it)', () => {
		const prev = S({ isStreaming: true });
		const next = S({ isStreaming: false });
		expect(decideBreakdownRefresh(prev, next)).toBe('refresh');
	});

	it('clears when the popover closes or there is no session', () => {
		expect(decideBreakdownRefresh(S(), S({ open: false }))).toBe('clear');
		expect(decideBreakdownRefresh(S(), S({ sessionId: null }))).toBe('clear');
	});

	it('re-fetches when the session changes under an open popover', () => {
		const prev = S({ sessionId: 'sess-1' });
		const next = S({ sessionId: 'sess-2' });
		expect(decideBreakdownRefresh(prev, next)).toBe('open-fetch');
	});

	it('stays idle while a turn streams — the display source never flips', () => {
		// The effect only ever sees these three fields; a streaming poll tick that
		// updates token counts leaves them unchanged, so the decision is `idle`
		// (no reset, no refetch) for every tick of the turn.
		const streaming = S({ isStreaming: true });
		// Simulate many poll ticks over the same open, streaming session.
		const actions = Array.from({ length: 5 }, () =>
			decideBreakdownRefresh(streaming, streaming)
		);
		expect(actions).toEqual(['idle', 'idle', 'idle', 'idle', 'idle']);
	});

	it('does not re-fetch or reset just because the popover stays open at idle', () => {
		const prev = S();
		const next = S();
		expect(decideBreakdownRefresh(prev, next)).toBe('idle');
	});

	it('models a full open→stream→turn-end sequence: one fetch on open, one on turn-end, none mid-stream', () => {
		// Drive the exact transition sequence the component walks through and record
		// which steps actually trigger a network fetch. The breakdown must be the
		// stable single source: fetched at open and turn-end, never flip-flopped.
		const steps: BreakdownRefreshState[] = [
			S({ open: false, isStreaming: false }), // initial (closed)
			S({ open: true, isStreaming: false }), //  user opens popover        -> open-fetch
			S({ open: true, isStreaming: true }), //   turn starts streaming      -> idle
			S({ open: true, isStreaming: true }), //   poll tick (tokens change)  -> idle
			S({ open: true, isStreaming: true }), //   poll tick                  -> idle
			S({ open: true, isStreaming: false }), //  turn ends                  -> refresh
			S({ open: true, isStreaming: false }), //  post-turn idle             -> idle
		];
		const actions: string[] = [];
		for (let i = 1; i < steps.length; i++) {
			actions.push(decideBreakdownRefresh(steps[i - 1], steps[i]));
		}
		expect(actions).toEqual(['open-fetch', 'idle', 'idle', 'idle', 'refresh', 'idle']);
		// Exactly two fetches (open + turn-end), and zero mid-stream flip-flops.
		const fetches = actions.filter((a) => a === 'open-fetch' || a === 'refresh');
		expect(fetches).toHaveLength(2);
		expect(actions.filter((a) => a === 'clear')).toHaveLength(0);
	});
});
