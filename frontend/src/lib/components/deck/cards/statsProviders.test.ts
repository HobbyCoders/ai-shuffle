import { describe, expect, it } from 'vitest';
import type { ProviderUsage } from '$lib/api/analytics';
import { billingLabel, providerCostCell, providerTotals } from './statsProviders';

function usage(overrides: Partial<ProviderUsage> = {}): ProviderUsage {
	return {
		key: 'p-1',
		name: 'Provider',
		provider_type: 'openai',
		billing: 'api',
		total_tokens_in: 1000,
		total_tokens_out: 100,
		cache_read_tokens: 0,
		cache_creation_tokens: 0,
		total_cost_usd: 1.5,
		cost_status: 'estimated',
		query_count: 4,
		estimated_query_count: 0,
		unpriced_query_count: 0,
		subscription_query_count: 0,
		api_query_count: 4,
		inferred_query_count: 0,
		...overrides
	};
}

describe('providerCostCell', () => {
	it('shows a priced API provider as a plain estimate', () => {
		const cell = providerCostCell(usage());
		expect(cell).toMatchObject({ text: '$1.50', note: null, missing: false });
	});

	it('never shows an unpriced provider as $0', () => {
		const cell = providerCostCell(usage({ total_cost_usd: 0, cost_status: 'unknown', unpriced_query_count: 4 }));
		expect(cell.text).toBe('No price');
		expect(cell.missing).toBe(true);
		expect(cell.title).toMatch(/unknown \(not zero\)/);
	});

	it('labels subscription usage as an API-price equivalent', () => {
		const cell = providerCostCell(usage({ billing: 'subscription' }));
		expect(cell.text).toBe('$1.50');
		expect(cell.note).toBe('API equiv.');
		expect(cell.title).toMatch(/subscription/i);
	});

	it('counts the unpriced turns a partial total leaves out', () => {
		const cell = providerCostCell(usage({ cost_status: 'partial', unpriced_query_count: 1 }));
		expect(cell.note).toBe('+1 unpriced');
		expect(cell.title).toMatch(/1 turn has no known price and is not included/);
		const both = providerCostCell(usage({ billing: 'subscription', cost_status: 'partial', unpriced_query_count: 3 }));
		expect(both.note).toBe('API equiv. · +3 unpriced');
	});
});

describe('billingLabel', () => {
	it('names every billing kind and falls back for unexpected values', () => {
		expect(billingLabel('subscription')).toBe('Subscription');
		expect(billingLabel('api')).toBe('API');
		expect(billingLabel('mixed')).toBe('Mixed');
		expect(billingLabel('unknown')).toBe('Billing unknown');
		expect(billingLabel('weird' as never)).toBe('Billing unknown');
	});
});

describe('providerTotals', () => {
	it('sums the unpriced and inferred turns across providers', () => {
		const totals = providerTotals([
			usage({ unpriced_query_count: 2, inferred_query_count: 1 }),
			usage({ key: 'claude', unpriced_query_count: 0, inferred_query_count: 5 })
		]);
		expect(totals).toEqual({ unpriced: 2, inferred: 6 });
	});
});
