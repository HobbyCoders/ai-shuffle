/**
 * Display rules for the Stats page's per-provider rows.
 *
 * A provider's cost can be missing (its model has no price), partly missing,
 * or an API-price estimate of subscription usage. None of those may read as a
 * plain "$0.00", so the cell text and its tooltip are decided here, where the
 * rules can be unit tested.
 */
import type { ProviderBilling, ProviderUsage } from '$lib/api/analytics';

export function formatCost(cost: number): string {
	if (cost >= 100) return '$' + cost.toFixed(0);
	if (cost >= 1) return '$' + cost.toFixed(2);
	return '$' + cost.toFixed(4);
}

export function formatTokens(n: number): string {
	if (n >= 1000000) return (n / 1000000).toFixed(2) + 'M';
	if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
	return n.toLocaleString();
}

const BILLING_LABELS: Record<ProviderBilling, string> = {
	subscription: 'Subscription',
	api: 'API',
	mixed: 'Mixed',
	unknown: 'Billing unknown'
};

export function billingLabel(billing: ProviderBilling): string {
	return BILLING_LABELS[billing] ?? BILLING_LABELS.unknown;
}

export interface ProviderCostCell {
	/** Main cell text. */
	text: string;
	/** Short qualifier under/after the amount, or null. */
	note: string | null;
	/** Tooltip explaining what the number is. */
	title: string;
	/** True when there is no amount to show (rendered muted). */
	missing: boolean;
}

export function providerCostCell(item: ProviderUsage): ProviderCostCell {
	if (item.cost_status === 'unknown') {
		return {
			text: 'No price',
			note: null,
			title: 'This model has no known price, so its cost is unknown (not zero).',
			missing: true
		};
	}
	const text = formatCost(item.total_cost_usd);
	const notes: string[] = [];
	const titles: string[] = ['Estimated by this app from token counts and model prices.'];
	if (item.billing === 'subscription') {
		notes.push('API equiv.');
		titles.push('Billed by subscription: this is what the same usage would cost at API prices.');
	} else if (item.billing === 'mixed') {
		titles.push('Part of this usage was on a subscription and is priced at API rates.');
	}
	if (item.cost_status === 'partial') {
		const n = item.unpriced_query_count;
		notes.push(`+${n} unpriced`);
		titles.push(`${n} ${n === 1 ? 'turn has' : 'turns have'} no known price and ${n === 1 ? 'is' : 'are'} not included.`);
	}
	return { text, note: notes.length ? notes.join(' · ') : null, title: titles.join(' '), missing: false };
}

/** Totals the Stats tiles need from the provider rows. */
export function providerTotals(items: ProviderUsage[]) {
	let unpriced = 0;
	let inferred = 0;
	for (const item of items) {
		unpriced += item.unpriced_query_count;
		inferred += item.inferred_query_count;
	}
	return { unpriced, inferred };
}
