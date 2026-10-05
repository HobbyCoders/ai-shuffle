/**
 * Pure helpers for the context-usage pill + its breakdown popover.
 *
 * Kept framework-free so the breakdown math and token formatting can be unit
 * tested without mounting the Svelte component.
 */

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
	/** Segment/percent width relative to contextMax, clamped 0–100 so rows are comparable. */
	barPercent: number;
	/** Muted caption after the row label, or null for none. */
	subline: string | null;
	/**
	 * True when this row has no real value to show yet (a fresh session with zero
	 * usage): the popover renders an em-dash and a neutral swatch instead of "0".
	 */
	empty: boolean;
	/**
	 * Resolved swatch/segment colour — the SDK category colour when valid, else a
	 * fallback-palette hue by row index (grayed rows and em-dash placeholders use a
	 * neutral muted colour). Shared by the swatch and the segmented bar.
	 */
	color: string;
	/**
	 * Grayed/muted row (the trailing "Free space" row): rendered in muted styling
	 * and EXCLUDED from the segmented progress bar (it is the bare track).
	 */
	muted: boolean;
	/**
	 * True when this row's tokens are part of `totalUsed` and should render a
	 * segment in the segmented progress bar. False for rows that are muted,
	 * empty (em-dash placeholder), or — on the estimate path — the Output
	 * tokens row, which is session-cumulative and NOT part of context used
	 * (`totalUsed = input + cacheCreation + cacheRead`).
	 * The segmented bar must build its segments from this flag rather than a
	 * `!muted && !empty` heuristic, or Output would inflate the bar past the
	 * header percentage.
	 */
	inBar: boolean;
	/**
	 * False only for deferred SDK categories (e.g. "MCP tools (deferred)"):
	 * those token pools sit outside the context-window accounting, so the
	 * popover renders an em-dash in place of a percent — same treatment as
	 * Claude Code's own panel. True (the default) for every other row.
	 */
	showPercent: boolean;
}

export interface ContextBreakdown {
	/** Conversation tokens — the pill's number. Excludes the autocompact buffer. */
	totalUsed: number;
	contextMax: number;
	/** Clamped 0–100. */
	totalPercent: number;
	/**
	 * More is in context than the window holds (a smaller window was picked
	 * after the chat grew). The percent stops at 100, so this is what tells
	 * the reader the used figure is past the limit rather than at it.
	 */
	overLimit: boolean;
	severity: UsageSeverity;
	rows: ContextBreakdownRow[];
}

/**
 * Fallback category swatch/segment palette — theme-agnostic mid-tone hues that
 * stay legible on both light and dark backgrounds. Used verbatim for the estimate
 * path and as the fallback when an SDK category has no parseable colour. Assigned
 * by row index (wrapping).
 */
export const CONTEXT_CATEGORY_PALETTE = [
	'#5b8def', // blue
	'#3bb27a', // green
	'#e0a13c', // amber
	'#d76b6b', // red
	'#9a7fd6', // violet
	'#39b3c0', // teal
	'#dd6fa0', // pink
	'#8894a6', // slate
];

/** Neutral swatch for grayed "Free space" rows and fresh-chat em-dash placeholders. */
const MUTED_SWATCH = 'color-mix(in oklch, var(--foreground) 12%, var(--background))';

/**
 * Resolve a category swatch colour: the SDK-provided string verbatim when it is a
 * hex (`#rgb`…`#rrggbbaa`) or an `rgb(`/`hsl(` colour, otherwise a palette hue by
 * row index (wrapping). Keeps the swatch and the segmented bar in sync.
 */
export function resolveCategoryColor(raw: string | undefined | null, index: number): string {
	if (raw && (/^#[0-9a-fA-F]{3,8}$/.test(raw) || raw.startsWith('rgb(') || raw.startsWith('hsl('))) {
		return raw;
	}
	return CONTEXT_CATEGORY_PALETTE[index % CONTEXT_CATEGORY_PALETTE.length];
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

/** Usage severity thresholds: ≥70 warning, >90 danger. */
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

	const totalUsed = Math.max(rawContextUsed, 0);
	const totalPercent = barPercent(totalUsed, contextMax);

	// Fresh session: no conversation tokens of any kind have landed yet. The
	// per-category rows have nothing real to show, so they render as em-dashes with
	// neutral swatches, and no "Free space" row is derived (the whole window is free).
	const noUsageYet =
		Math.max(rawContextUsed, 0) === 0 &&
		inputTokens === 0 &&
		outputTokens === 0 &&
		cacheCreationTokens === 0 &&
		cacheReadTokens === 0;

	const rows: ContextBreakdownRow[] = [
		{
			key: 'input',
			label: 'Input tokens',
			tokens: inputTokens,
			barPercent: barPercent(inputTokens, contextMax),
			subline: null,
			empty: noUsageYet,
			color: noUsageYet ? MUTED_SWATCH : resolveCategoryColor(null, 0),
			muted: false,
			inBar: !noUsageYet,
			showPercent: true,
		},
		{
			key: 'cacheRead',
			label: 'Cache read',
			tokens: cacheReadTokens,
			barPercent: barPercent(cacheReadTokens, contextMax),
			subline: null,
			empty: noUsageYet,
			color: noUsageYet ? MUTED_SWATCH : resolveCategoryColor(null, 1),
			muted: false,
			inBar: !noUsageYet,
			showPercent: true,
		},
		{
			key: 'cacheCreation',
			label: 'Cache creation',
			tokens: cacheCreationTokens,
			barPercent: barPercent(cacheCreationTokens, contextMax),
			subline: null,
			empty: noUsageYet,
			color: noUsageYet ? MUTED_SWATCH : resolveCategoryColor(null, 2),
			muted: false,
			inBar: !noUsageYet,
			showPercent: true,
		},
		{
			key: 'output',
			label: 'Output tokens',
			tokens: outputTokens,
			barPercent: barPercent(outputTokens, contextMax),
			subline: 'session total · not in context',
			empty: noUsageYet,
			color: noUsageYet ? MUTED_SWATCH : resolveCategoryColor(null, 3),
			muted: false,
			// Session-cumulative, NOT part of totalUsed — never a bar segment.
			inBar: false,
			showPercent: true,
		},
	];

	// Grayed "Free space" row (always last) — only once real usage has landed.
	if (!noUsageYet) {
		const freeTokens = Math.max(contextMax - totalUsed, 0);
		rows.push({
			key: 'freeSpace',
			label: 'Free space',
			tokens: freeTokens,
			barPercent: barPercent(freeTokens, contextMax),
			subline: null,
			empty: false,
			color: MUTED_SWATCH,
			muted: true,
			inBar: false,
			showPercent: true,
		});
	}

	return {
		totalUsed,
		contextMax,
		totalPercent,
		overLimit: contextMax > 0 && totalUsed > contextMax,
		severity: usageSeverity(totalPercent),
		rows,
	};
}

/**
 * Inputs the ContextUsageBar effect watches to decide when to (re)fetch the SDK
 * `/context-usage` breakdown. Deliberately tiny so a streaming token update — which
 * touches many other `$activeSession` fields but none of these — never triggers a
 * refetch. Keeping this out of the component makes the refresh cadence unit-testable.
 */
export interface BreakdownRefreshState {
	/** Popover open? Opening triggers a fetch if none is loaded/in-flight yet. */
	open: boolean;
	sessionId: string | null;
	/** The active turn is streaming — the SDK breakdown can't refresh mid-turn. */
	isStreaming: boolean;
}

/**
 * What the effect should do about the rich breakdown given the previous and next
 * watched state:
 * - `clear`         — no session: drop any breakdown, cancel fetches.
 * - `session-fetch` — the session changed (or first appeared, incl. on mount):
 *                     synchronously drop the old breakdown (no cross-session flash),
 *                     abort any in-flight fetch, and fetch the new session's — even
 *                     while the popover is closed, so the pill ring is accurate.
 * - `open-fetch`    — popover just opened for the same session: fetch only if none
 *                     is already loaded/in-flight (the component guards that).
 * - `refresh`       — a turn just ended: fetch fresh (even while closed), keeping the
 *                     current breakdown on screen until the new one lands.
 * - `idle`          — nothing relevant changed (e.g. a streaming poll tick, or the
 *                     popover merely closing): do NOT refetch and do NOT reset.
 */
export type BreakdownRefreshAction = 'clear' | 'session-fetch' | 'open-fetch' | 'refresh' | 'idle';

/**
 * Decide the breakdown refresh action from the watched state transition.
 *
 * The rich breakdown is now fetched on session load (not just popover open) so the
 * pill ring can prefer it: a sessionId change fetches even while closed, a turn end
 * refreshes even while closed, and opening fetches only when nothing is loaded yet.
 * Streaming poll updates (which change token fields but NOT open/sessionId/isStreaming)
 * map to `idle`, so nothing oscillates while a turn streams.
 */
export function decideBreakdownRefresh(
	prev: BreakdownRefreshState,
	next: BreakdownRefreshState
): BreakdownRefreshAction {
	if (!next.sessionId) return 'clear';
	// The session changed (or first appeared / mounted): reset + fetch the new one.
	if (prev.sessionId !== next.sessionId) return 'session-fetch';
	// Popover just opened for the same session — fetch if nothing is loaded yet.
	if (!prev.open && next.open) return 'open-fetch';
	// A turn just finished — the breakdown is now stale, pull a fresh one.
	if (prev.isStreaming && !next.isStreaming) return 'refresh';
	return 'idle';
}

/**
 * Map the live SDK ContextUsageResponse.usage into the same display shape the
 * popover already renders, so the component can swap the estimate for the real
 * per-category breakdown without any layout change.
 *
 * - total: the SDK's own `usage.totalTokens` / `usage.percentage`, used
 *   verbatim — both already exclude the "Autocompact buffer" category, and the
 *   buffer is a reserve, not consumed context, so it is never folded in. The
 *   existing severity thresholds are applied on top of the SDK percentage.
 * - rows: one per category (label = name). Deferred categories (`isDeferred`,
 *   e.g. "MCP tools (deferred)") sit OUTSIDE the context-window accounting — like
 *   Claude Code's own panel, they render grayed with an em-dash instead of a
 *   percent (`showPercent: false`), never contribute a segmented-bar segment
 *   (`inBar: false`), and are ordered LAST, after Free space. Zero/negative-token
 *   categories are dropped EXCEPT "Free space", which is always kept, grayed, and
 *   ordered right before the deferred categories. The "Autocompact buffer"
 *   category is dropped entirely — it is not a row, not a bar segment, and not
 *   part of any total. Non-free, non-deferred categories are ordered by
 *   descending token count, then Free space, then deferred categories (also
 *   descending token count).
 */
export function mapSdkContextUsage(usage: SdkContextUsage): ContextBreakdown {
	// An unknown window arrives as null (a native leg on a model with no
	// resolvable context window). Normalise to 0 — the same "unknown" sentinel
	// the rest of this module already handles (barPercent returns 0, the
	// popover renders "0" rather than throwing). Leaving it null would reach
	// formatTokenCount via contextMax and throw on null.toString(), taking the
	// whole pill down, not just the popover.
	const max = usage.maxTokens ?? 0;
	const isAutocompact = (name: string) => name.toLowerCase() === 'autocompact buffer';
	const isFreeSpace = (name: string) => name === 'Free space';

	// The autocompact buffer is a reserve, not consumed context: it is dropped
	// from the rows, the bar and every total. `usage.totalTokens` already
	// excludes it, so it is the header number verbatim, and `usage.percentage`
	// (the CLI's own figure) is the ring/header percent as reported.
	const totalUsed = usage.totalTokens;
	const totalPercent =
		usage.percentage != null
			? Math.min(Math.max(usage.percentage, 0), 100)
			: barPercent(totalUsed, max);

	const kept = usage.categories
		.filter((c) => !isAutocompact(c.name))
		.filter((c) => c.tokens > 0 || isFreeSpace(c.name));
	// Non-free, non-deferred categories descending by tokens, then Free space,
	// then deferred categories (also descending by tokens) last.
	const nonFree = kept
		.filter((c) => !isFreeSpace(c.name) && !c.isDeferred)
		.sort((a, b) => b.tokens - a.tokens);
	const free = kept.filter((c) => isFreeSpace(c.name));
	const deferred = kept
		.filter((c) => !isFreeSpace(c.name) && c.isDeferred)
		.sort((a, b) => b.tokens - a.tokens);
	const ordered = [...nonFree, ...free, ...deferred];

	const rows: ContextBreakdownRow[] = ordered.map((c, i) => {
		const isFree = isFreeSpace(c.name);
		const isDeferred = !!c.isDeferred;
		const muted = isFree || isDeferred;
		return {
			key: c.name,
			label: c.name,
			tokens: c.tokens,
			barPercent: barPercent(c.tokens, max),
			subline: isDeferred ? 'deferred' : null,
			empty: false,
			color: muted ? MUTED_SWATCH : resolveCategoryColor(c.color, i),
			muted,
			// Free space and deferred categories never contribute a segmented-bar
			// segment — deferred token pools sit outside the context-window
			// accounting entirely, so counting them would inflate the bar past
			// the header percentage.
			inBar: !isFree && !isDeferred,
			// Deferred rows show an em-dash instead of a percent, matching
			// Claude Code's own panel (their tokens aren't part of the window).
			showPercent: !isDeferred,
		};
	});

	return {
		totalUsed,
		contextMax: max,
		totalPercent,
		overLimit: max > 0 && totalUsed > max,
		severity: usageSeverity(totalPercent),
		rows,
	};
}

/** The window the meter measures against, and the one the next turn uses when that differs. */
export interface ContextWindowResolution {
	window: number;
	/** Set only while a running turn still uses a window other than the picked one. */
	nextWindow: number | null;
}

/**
 * Pick the meter's denominator.
 *
 * - `selected`: the chat's picked window for its model (`contextWindowForChoice`),
 *   or null when the model offers no choice. It is what the next turn sends, so
 *   it wins over an older report — a pick shows as soon as it is acknowledged.
 * - `reported`: the window the backend measured the current numbers against
 *   (the SDK/persisted breakdown's `maxTokens`), or null/0 when unknown.
 * - `capacity`: the model's own window from the local registry, the last resort.
 *
 * A pick applies from the next turn. While a turn is still running on another
 * window (`reportedIsCurrentTurn`), its numbers are measured against that
 * window, so the meter keeps it and returns the picked one as `nextWindow`.
 */
export function resolveContextWindow(input: {
	selected: number | null;
	reported: number | null;
	capacity: number;
	reportedIsCurrentTurn: boolean;
}): ContextWindowResolution {
	const reported = input.reported != null && input.reported > 0 ? input.reported : null;
	const next = input.selected ?? reported ?? input.capacity;
	if (input.reportedIsCurrentTurn && reported != null && reported !== next) {
		return { window: reported, nextWindow: next };
	}
	return { window: next, nextWindow: null };
}

/**
 * Re-measure a breakdown against another window: the total percent, every
 * row's share, the Free space row and the severity. Token counts other than
 * Free space are facts about the conversation and stay as they are.
 */
export function fitBreakdownToWindow(breakdown: ContextBreakdown, contextMax: number): ContextBreakdown {
	if (contextMax <= 0 || breakdown.contextMax === contextMax) return breakdown;
	const totalPercent = barPercent(breakdown.totalUsed, contextMax);
	const freeTokens = Math.max(contextMax - breakdown.totalUsed, 0);
	return {
		...breakdown,
		contextMax,
		totalPercent,
		overLimit: breakdown.totalUsed > contextMax,
		severity: usageSeverity(totalPercent),
		rows: breakdown.rows.map((row) => {
			const tokens = row.key === 'freeSpace' || row.key === 'Free space' ? freeTokens : row.tokens;
			return { ...row, tokens, barPercent: barPercent(tokens, contextMax) };
		}),
	};
}
