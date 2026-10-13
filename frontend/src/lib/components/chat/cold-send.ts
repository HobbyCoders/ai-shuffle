/**
 * The warning before a costly send (#1831).
 *
 * When a chat's context is large and its provider cache has gone cold
 * (`GET /sessions/{id}/cache-state` says `warn`), the next message makes the
 * provider read the whole conversation again at full price. The composer says
 * so in plain words and offers "Compact first" beside "Send anyway". Thresholds
 * live on the backend (app/core/send_cost.py): 150k tokens, and a cache
 * lifetime of an hour for Claude Code and five minutes for the other providers.
 *
 * "Send anyway" is remembered per chat, with the context size it was given
 * for, until the context changes materially (`materiallyChanged`).
 */
import type { CacheState } from '$lib/api/limit-resume';

const ACK_KEY = 'aishuffle.coldSendAck';
/** A change of at least this many tokens, or this fraction, is material. */
export const MATERIAL_TOKENS = 50_000;
export const MATERIAL_RATIO = 0.25;

/** "410k tokens", "1.2M tokens". */
export function tokensInWords(tokens: number): string {
	if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1).replace(/\.0$/, '')}M tokens`;
	return `${Math.round(tokens / 1000)}k tokens`;
}

/** "2 h", "45 min". */
export function idleInWords(seconds: number): string {
	const minutes = Math.round(seconds / 60);
	if (minutes < 60) return `${Math.max(1, minutes)} min`;
	const hours = Math.round(minutes / 6) / 10;
	return `${hours.toString().replace(/\.0$/, '')} h`;
}

/** The notice text, or null when there is nothing to say. */
export function coldSendText(state: CacheState): string | null {
	if (!state.warn || !state.context_tokens) return null;
	const idle = state.idle_seconds ? ` after ${idleInWords(state.idle_seconds)} idle` : '';
	const intro = `This chat holds about ${tokensInWords(state.context_tokens)} and its cache has expired${idle}. `;
	const cost = state.cost_comparison;
	if (!cost) return intro + 'Sending now re-reads all of it. Cost comparison unavailable: model pricing is missing. Compact first to reduce context, or Send anyway.';
	const usd = (value: number) => `$${value.toFixed(2)}`;
	const summary = cost.summary_tokens
		? ` and an assumed ${tokensInWords(cost.summary_tokens).replace(' tokens', '-token')} summary` : '';
	const cacheWrite = state.leg === 'delegated' ? ' Includes 1-hour cache writes; no cache hits assumed.' : ' No cache hits assumed.';
	const recommendation = cost.recommendation === 'compact'
		? 'Recommendation: Compact first for the lower estimated total.'
		: 'Recommendation: Send anyway for this one call; compacting may save on later calls.';
	return intro +
		`Cold resume total: ~${usd(cost.cold_resume_usd)}. ` +
		`Compact and continue total: ~${usd(cost.compact_continue_usd)} ` +
		`(${usd(cost.compaction_usd)} compaction + ${usd(cost.continued_usd)} continuation). ` +
		`API-price estimates for ${cost.model}, assuming ~${tokensInWords(cost.compacted_context_tokens)} retained${summary} ` +
		`and a ${tokensInWords(cost.reply_tokens).replace(' tokens', '-token')} reply. Later calls are extra.` + cacheWrite + ' ' + recommendation;
}

export function materiallyChanged(acknowledged: number, current: number): boolean {
	const diff = Math.abs(current - acknowledged);
	return diff >= MATERIAL_TOKENS || diff >= acknowledged * MATERIAL_RATIO;
}

function readAll(): Record<string, number> {
	try {
		const raw = globalThis.localStorage?.getItem(ACK_KEY);
		const parsed: unknown = raw ? JSON.parse(raw) : {};
		return parsed && typeof parsed === 'object' ? (parsed as Record<string, number>) : {};
	} catch {
		return {};
	}
}

/** The context size "Send anyway" was given for in this chat, if any. */
export function acknowledgedTokens(sessionId: string): number | null {
	const value = readAll()[sessionId];
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

export function acknowledge(sessionId: string, tokens: number): void {
	try {
		const all = readAll();
		all[sessionId] = tokens;
		// Keep the newest 200 chats.
		const keys = Object.keys(all);
		for (const key of keys.slice(0, Math.max(0, keys.length - 200))) delete all[key];
		globalThis.localStorage?.setItem(ACK_KEY, JSON.stringify(all));
	} catch {
		/* storage full or unavailable: the warning shows again next time */
	}
}

/** Whether to warn before this send. */
export function shouldWarn(state: CacheState | null, acknowledged: number | null): boolean {
	if (!state?.warn || !state.context_tokens) return false;
	return acknowledged === null || materiallyChanged(acknowledged, state.context_tokens);
}
