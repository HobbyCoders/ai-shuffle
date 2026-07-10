/**
 * Pure helpers for the context-usage pill + its breakdown popover.
 *
 * Kept framework-free so the breakdown math and token formatting can be unit
 * tested without mounting the Svelte component.
 */

import { AUTO_COMPACTION_BASE } from '$lib/constants/chat';
import type { SdkContextUsage } from '$lib/api/sessions';

export type UsageSeverity = 'neutral' | 'warning' | 'danger';

export interface ContextUsageInput {
	/**
	 * Conversation context tokens = input + cacheCreation + cacheRead
	 * (i.e. `activeSession.contextUsed`, WITHOUT the system overhead reserve).
	 */
	rawContextUsed: number;
	inputTokens: number;
	/** Session-cumulative `total_tokens_out` — NOT a per-turn figure like the other rows. */
	outputTokens: number;
	cacheCreationTokens: number;
	cacheReadTokens: number;
	/** Model context window (200k standard / 1M extended). */
	contextMax: number;
}

export interface ContextBreakdownRow {
	key: string;
	label: string;
	tokens: number;
	/** Bar width relative to contextMax, clamped 0–100 so rows are comparable. */
	barPercent: number;
	/** Muted caption under the row, or null for none. */
	subline: string | null;
}

export interface ContextBreakdown {
	/** System overhead reserve + conversation tokens — the pill's number. */
	totalUsed: number;
	contextMax: number;
	/** Clamped 0–100. */
	totalPercent: number;
	severity: UsageSeverity;
	rows: ContextBreakdownRow[];
}

/**
 * Compact token count: 1234 → "1.2k", 1500000 → "1.5M", <1000 → raw.
 * Trailing ".0" is dropped (200000 → "200k") for a cleaner readout.
 */
export function formatTokenCount(count: number): string {
	if (count >= 1_000_000) {
		return `${(count / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`;
	}
	if (count >= 1000) {
		return `${(count / 1000).toFixed(1).replace(/\.0$/, '')}k`;
	}
	return count.toString();
}

/** Severity thresholds mirror UsageLimitIndicator: ≥70 warning, >90 danger. */
export function usageSeverity(percent: number): UsageSeverity {
	if (percent > 90) return 'danger';
	if (percent >= 70) return 'warning';
	return 'neutral';
}

function barPercent(tokens: number, contextMax: number): number {
	if (contextMax <= 0) return 0;
	return Math.min(Math.max((tokens / contextMax) * 100, 0), 100);
}

/**
 * Build the total + per-token-type breakdown shown in the popover.
 * The total matches the pill exactly (system overhead + conversation tokens).
 * Output tokens are surfaced for context but are NOT part of the context sum;
 * unlike the other rows, outputTokens is a session-cumulative count, not per-turn.
 */
export function computeContextBreakdown(input: ContextUsageInput): ContextBreakdown {
	const {
		rawContextUsed,
		inputTokens,
		outputTokens,
		cacheCreationTokens,
		cacheReadTokens,
		contextMax,
	} = input;

	const totalUsed = AUTO_COMPACTION_BASE + Math.max(rawContextUsed, 0);
	const totalPercent = barPercent(totalUsed, contextMax);

	const rows: ContextBreakdownRow[] = [
		{
			key: 'input',
			label: 'Input tokens',
			tokens: inputTokens,
			barPercent: barPercent(inputTokens, contextMax),
			subline: null,
		},
		{
			key: 'cacheRead',
			label: 'Cache read',
			tokens: cacheReadTokens,
			barPercent: barPercent(cacheReadTokens, contextMax),
			subline: null,
		},
		{
			key: 'cacheCreation',
			label: 'Cache creation',
			tokens: cacheCreationTokens,
			barPercent: barPercent(cacheCreationTokens, contextMax),
			subline: null,
		},
		{
			key: 'output',
			label: 'Output tokens',
			tokens: outputTokens,
			barPercent: barPercent(outputTokens, contextMax),
			subline: 'session total · not in context',
		},
		{
			key: 'overhead',
			label: 'System overhead',
			tokens: AUTO_COMPACTION_BASE,
			barPercent: barPercent(AUTO_COMPACTION_BASE, contextMax),
			subline: 'estimated',
		},
	];

	return {
		totalUsed,
		contextMax,
		totalPercent,
		severity: usageSeverity(totalPercent),
		rows,
	};
}

/**
 * Map the live SDK ContextUsageResponse.usage into the same display shape the
 * popover already renders, so the component can swap the estimate for the real
 * per-category breakdown without any layout change.
 *
 * - total: usage.totalTokens / usage.maxTokens, percentage clamped 0–100, with
 *   the existing severity thresholds.
 * - rows: one per category (label = name, bar relative to maxTokens). Deferred
 *   categories get a "deferred" subline. Zero/negative-token categories are
 *   dropped EXCEPT "Free space", which is always kept when present.
 */
export function mapSdkContextUsage(usage: SdkContextUsage): ContextBreakdown {
	const max = usage.maxTokens;
	const totalPercent = Math.min(Math.max(usage.percentage, 0), 100);

	const rows: ContextBreakdownRow[] = usage.categories
		.filter((c) => c.tokens > 0 || c.name === 'Free space')
		.map((c) => ({
			key: c.name,
			label: c.name,
			tokens: c.tokens,
			barPercent: barPercent(c.tokens, max),
			subline: c.isDeferred ? 'deferred' : null,
		}));

	return {
		totalUsed: usage.totalTokens,
		contextMax: max,
		totalPercent,
		severity: usageSeverity(totalPercent),
		rows,
	};
}
