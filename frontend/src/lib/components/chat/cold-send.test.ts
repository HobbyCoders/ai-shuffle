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
	it('says the size and the idle time plainly', () => {
		expect(tokensInWords(412_000)).toBe('412k tokens');
		expect(tokensInWords(1_200_000)).toBe('1.2M tokens');
		expect(idleInWords(5400)).toBe('1.5 h');
		expect(idleInWords(600)).toBe('10 min');
		expect(coldSendText(state())).toBe(
			'This chat holds about 412k tokens and its cache has expired after 1.5 h idle. ' +
				'Sending now re-reads all of it at full price. Compacting first makes it much smaller.'
		);
		expect(coldSendText(state({ warn: false }))).toBeNull();
	});
});
