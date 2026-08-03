/**
 * Unit tests for the client-side estimated-spend catalog (#757).
 *
 * Pure functions only — no network, no provider calls. Covers: provider-type
 * gating (only openai/anthropic are priced), prefix-match model resolution
 * (including dated model-id suffixes), the input/output token formula, and
 * the `null`-on-unknown-model contract that keeps the UI from rendering a
 * misleading `$0.00`.
 */
import { describe, expect, it } from 'vitest';
import { estimateSpendUsd, formatEstimateUsd } from './provider-pricing';

const ZERO_USAGE = {
	inputTokens: 0,
	outputTokens: 0,
	cacheCreationTokens: 0,
	cacheReadTokens: 0
};

describe('estimateSpendUsd', () => {
	it('returns null for a non-priced provider type (e.g. openrouter, chatgpt)', () => {
		expect(estimateSpendUsd('openrouter', 'gpt-4o', { ...ZERO_USAGE, inputTokens: 1000 })).toBeNull();
		expect(estimateSpendUsd('chatgpt', 'gpt-4o', { ...ZERO_USAGE, inputTokens: 1000 })).toBeNull();
		expect(estimateSpendUsd(null, 'gpt-4o', { ...ZERO_USAGE, inputTokens: 1000 })).toBeNull();
	});

	it('returns null for an unrecognized model id (never guesses a price)', () => {
		expect(
			estimateSpendUsd('openai', 'some-future-model-nobody-has-heard-of', {
				...ZERO_USAGE,
				inputTokens: 1000
			})
		).toBeNull();
	});

	it('computes an OpenAI estimate from input + output tokens at catalog rates', () => {
		// gpt-4o: $2.50 / 1M input, $10.00 / 1M output.
		const usd = estimateSpendUsd('openai', 'gpt-4o', {
			inputTokens: 1_000_000,
			outputTokens: 1_000_000,
			cacheCreationTokens: 0,
			cacheReadTokens: 0
		});
		expect(usd).toBeCloseTo(2.5 + 10.0, 6);
	});

	it('matches a dated model-id suffix by longest known prefix', () => {
		const usd = estimateSpendUsd('openai', 'gpt-4o-2024-08-06', {
			inputTokens: 1_000_000,
			outputTokens: 0,
			cacheCreationTokens: 0,
			cacheReadTokens: 0
		});
		expect(usd).toBeCloseTo(2.5, 6);
	});

	it('prefers the longer/more specific prefix match (gpt-4o-mini over gpt-4o)', () => {
		const usd = estimateSpendUsd('openai', 'gpt-4o-mini-2024-07-18', {
			inputTokens: 1_000_000,
			outputTokens: 0,
			cacheCreationTokens: 0,
			cacheReadTokens: 0
		});
		// gpt-4o-mini: $0.15/1M input — NOT gpt-4o's $2.50/1M.
		expect(usd).toBeCloseTo(0.15, 6);
	});

	it('folds cache creation + cache read tokens into the input-like bucket', () => {
		const usd = estimateSpendUsd('anthropic', 'claude-3-5-sonnet', {
			inputTokens: 0,
			outputTokens: 0,
			cacheCreationTokens: 500_000,
			cacheReadTokens: 500_000
		});
		// claude-3-5-sonnet: $3.00/1M input-like tokens.
		expect(usd).toBeCloseTo(3.0, 6);
	});

	it('computes an Anthropic estimate from input + output tokens at catalog rates', () => {
		// claude-3-5-sonnet: $3.00/1M input, $15.00/1M output.
		const usd = estimateSpendUsd('anthropic', 'claude-3-5-sonnet-20241022', {
			inputTokens: 1_000_000,
			outputTokens: 1_000_000,
			cacheCreationTokens: 0,
			cacheReadTokens: 0
		});
		expect(usd).toBeCloseTo(3.0 + 15.0, 6);
	});

	it('returns 0 (not null) for a known model with zero usage', () => {
		expect(estimateSpendUsd('openai', 'gpt-4o', ZERO_USAGE)).toBe(0);
	});

	it('clamps negative token counts to zero rather than producing a negative estimate', () => {
		const usd = estimateSpendUsd('openai', 'gpt-4o', {
			inputTokens: -100,
			outputTokens: -100,
			cacheCreationTokens: 0,
			cacheReadTokens: 0
		});
		expect(usd).toBe(0);
	});
});

describe('formatEstimateUsd', () => {
	it('formats zero as $0.00', () => {
		expect(formatEstimateUsd(0)).toBe('$0.00');
	});

	it('formats sub-cent amounts with 4 decimals so they are not truncated to $0.00', () => {
		expect(formatEstimateUsd(0.0034)).toBe('$0.0034');
	});

	it('formats amounts >= 1 cent with 2 decimals', () => {
		expect(formatEstimateUsd(1.2345)).toBe('$1.23');
		expect(formatEstimateUsd(0.01)).toBe('$0.01');
	});
});
