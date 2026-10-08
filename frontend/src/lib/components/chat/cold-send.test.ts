import { beforeEach, describe, expect, it } from 'vitest';
import type { CacheState } from '$lib/api/limit-resume';
import {
	acknowledge,
	acknowledgedTokens,
	coldSendText,
	idleInWords,
	materiallyChanged,
	shouldWarn,
	tokensInWords
} from './cold-send';

function state(over: Partial<CacheState> = {}): CacheState {
	return {
		leg: 'delegated',
		context_tokens: 412_000,
		last_call_at: 1,
		idle_seconds: 5400,
		cache_ttl_seconds: 3600,
		threshold_tokens: 150_000,
		cold: true,
		large: true,
		warn: true,
		...over
	};
}

beforeEach(() => localStorage.clear());

describe('when to warn', () => {
	it('warns on a large chat with a cold cache', () => {
		expect(shouldWarn(state(), null)).toBe(true);
	});

	it('does not warn on a small chat or a warm cache', () => {
		expect(shouldWarn(state({ warn: false, large: false, context_tokens: 20_000 }), null)).toBe(false);
		expect(shouldWarn(state({ warn: false, cold: false }), null)).toBe(false);
		expect(shouldWarn(null, null)).toBe(false);
	});

	it('remembers Send anyway until the context changes materially', () => {
		acknowledge('s1', 412_000);
		expect(acknowledgedTokens('s1')).toBe(412_000);
		expect(shouldWarn(state({ context_tokens: 430_000 }), acknowledgedTokens('s1'))).toBe(false);
		expect(shouldWarn(state({ context_tokens: 470_000 }), acknowledgedTokens('s1'))).toBe(true);
		// Another chat is unaffected.
		expect(shouldWarn(state(), acknowledgedTokens('s2'))).toBe(true);
	});

	it('treats 50k tokens or a quarter as material', () => {
		expect(materiallyChanged(400_000, 449_000)).toBe(false);
		expect(materiallyChanged(400_000, 450_000)).toBe(true);
		expect(materiallyChanged(160_000, 200_000)).toBe(true);
		expect(materiallyChanged(400_000, 300_000)).toBe(true);
	});
});

describe('the words', () => {
	it.each(['send', 'compact'] as const)('compares totals and recommends %s', (recommendation) => {
		const text = coldSendText(state({ cost_comparison: {
			model: 'claude-sonnet-5', cold_resume_usd: 1.69, compaction_usd: 1.73,
			continued_usd: 0.37, compact_continue_usd: 2.1,
			compacted_context_tokens: 82_400, reply_tokens: 4_000, summary_tokens: 8_000,
			recommendation
		} }))!;
		expect(text).toContain('Cold resume total: ~$1.69');
		expect(text).toContain('Compact and continue total: ~$2.10 ($1.73 compaction + $0.37 continuation)');
		expect(text).toContain('claude-sonnet-5, assuming ~82k tokens retained and an assumed 8k-token summary');
		expect(text).toContain('1-hour cache writes; no cache hits assumed');
		expect(text).toContain(recommendation === 'send'
			? 'Recommendation: Send anyway for this one call'
			: 'Recommendation: Compact first for the lower estimated total');
	});

	it('does not invent a model call or a cache-write charge for native compaction', () => {
		const text = coldSendText(state({ leg: 'openrouter', cost_comparison: {
			model: 'fixture', cold_resume_usd: 0.84, compaction_usd: 0,
			continued_usd: 0.2, compact_continue_usd: 0.2,
			compacted_context_tokens: 80_000, reply_tokens: 4_000, summary_tokens: 0,
			recommendation: 'compact'
		} }))!;
		expect(text).toContain('$0.00 compaction + $0.20 continuation');
		expect(text).not.toContain('summary');
		expect(text).not.toContain('cache writes');
	});

	it('says the size and the idle time plainly', () => {
		expect(tokensInWords(412_000)).toBe('412k tokens');
		expect(tokensInWords(1_200_000)).toBe('1.2M tokens');
		expect(idleInWords(5400)).toBe('1.5 h');
		expect(idleInWords(600)).toBe('10 min');
		expect(coldSendText(state())).toBe(
			'This chat holds about 412k tokens and its cache has expired after 1.5 h idle. ' +
				'Sending now re-reads all of it. Cost comparison unavailable: model pricing is missing. Compact first to reduce context, or Send anyway.'
		);
		expect(coldSendText(state({ warn: false }))).toBeNull();
	});
});
