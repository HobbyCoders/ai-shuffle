/**
 * Client-side estimated-spend catalog for OpenAI/Anthropic API-key providers
 * (GitHub issue #757 — the deferred "estimated spend" row of #696's tier
 * table). This is a rough, clearly-labeled ESTIMATE: `tokens × catalog price`,
 * computed entirely in the browser from token counts the app already tracks.
 * It is NOT a billing figure — model list prices drift and this catalog is
 * not kept in lockstep with either vendor's pricing page.
 *
 * Deliberately minimal (per #757 scope): a small lookup table of well-known
 * model-id prefixes to $/1M-token input/output rates, plus one pure function
 * that turns a token-usage tuple into a dollar estimate. Cache read/write
 * tokens are folded into the "input-like" bucket at the plain input rate —
 * both vendors price cache reads cheaper and Anthropic prices cache writes
 * pricier, but modeling that precisely is out of scope for an "est." chip;
 * folding them in keeps the estimate conservative-ish without a wrong-looking
 * separate line. Unknown models return `null` — the caller renders nothing
 * rather than guessing.
 */

export interface ModelPriceUsd {
	/** USD per 1,000,000 input tokens (list price, no caching discount). */
	inputPerMTok: number;
	/** USD per 1,000,000 output tokens. */
	outputPerMTok: number;
}

/** OpenAI platform (api.openai.com) API-key model prices, USD per 1M tokens. */
const OPENAI_PRICING: Record<string, ModelPriceUsd> = {
	'gpt-4o-mini': { inputPerMTok: 0.15, outputPerMTok: 0.6 },
	'gpt-4o': { inputPerMTok: 2.5, outputPerMTok: 10.0 },
	'gpt-4.1-nano': { inputPerMTok: 0.1, outputPerMTok: 0.4 },
	'gpt-4.1-mini': { inputPerMTok: 0.4, outputPerMTok: 1.6 },
	'gpt-4.1': { inputPerMTok: 2.0, outputPerMTok: 8.0 },
	'o4-mini': { inputPerMTok: 1.1, outputPerMTok: 4.4 },
	'o3-mini': { inputPerMTok: 1.1, outputPerMTok: 4.4 },
	'o3': { inputPerMTok: 2.0, outputPerMTok: 8.0 },
	'gpt-5-mini': { inputPerMTok: 0.25, outputPerMTok: 2.0 },
	'gpt-5': { inputPerMTok: 1.25, outputPerMTok: 10.0 }
};

/** Anthropic Messages API (api.anthropic.com) API-key model prices, USD per 1M tokens. */
const ANTHROPIC_PRICING: Record<string, ModelPriceUsd> = {
	'claude-3-5-haiku': { inputPerMTok: 0.8, outputPerMTok: 4.0 },
	'claude-haiku': { inputPerMTok: 0.8, outputPerMTok: 4.0 },
	'claude-3-5-sonnet': { inputPerMTok: 3.0, outputPerMTok: 15.0 },
	'claude-sonnet': { inputPerMTok: 3.0, outputPerMTok: 15.0 },
	'claude-3-opus': { inputPerMTok: 15.0, outputPerMTok: 75.0 },
	'claude-opus': { inputPerMTok: 15.0, outputPerMTok: 75.0 },
	'claude-fable': { inputPerMTok: 15.0, outputPerMTok: 75.0 }
};

/**
 * Resolve a model id/label to a catalog price by longest-matching known
 * prefix (case-insensitive substring match, since provider model ids often
 * carry date suffixes like `gpt-4o-2024-08-06` or `claude-3-5-sonnet-20241022`).
 * Returns `null` when nothing in the catalog matches.
 */
function resolvePrice(
	catalog: Record<string, ModelPriceUsd>,
	modelId: string | null | undefined
): ModelPriceUsd | null {
	if (!modelId) return null;
	const needle = modelId.toLowerCase();
	let best: ModelPriceUsd | null = null;
	let bestLen = -1;
	for (const [key, price] of Object.entries(catalog)) {
		if (needle.includes(key) && key.length > bestLen) {
			best = price;
			bestLen = key.length;
		}
	}
	return best;
}

/** Token counts used to compute an estimate — mirrors the context-usage fields. */
export interface SpendTokenUsage {
	inputTokens: number;
	outputTokens: number;
	cacheCreationTokens: number;
	cacheReadTokens: number;
}

/**
 * Estimate USD spend for a provider/model given token counts already tracked
 * elsewhere in the app. Returns `null` when the provider type isn't a priced
 * API-key provider or the model isn't in the (deliberately small) catalog —
 * callers should render nothing rather than a misleading `$0.00`.
 */
export function estimateSpendUsd(
	providerType: string | null | undefined,
	modelId: string | null | undefined,
	usage: SpendTokenUsage
): number | null {
	let catalog: Record<string, ModelPriceUsd> | null = null;
	if (providerType === 'openai') catalog = OPENAI_PRICING;
	else if (providerType === 'anthropic') catalog = ANTHROPIC_PRICING;
	if (!catalog) return null;

	const price = resolvePrice(catalog, modelId);
	if (!price) return null;

	const inputLike = Math.max(usage.inputTokens, 0) + Math.max(usage.cacheCreationTokens, 0) + Math.max(usage.cacheReadTokens, 0);
	const outputLike = Math.max(usage.outputTokens, 0);
	return (inputLike * price.inputPerMTok + outputLike * price.outputPerMTok) / 1_000_000;
}

/** Format a dollar estimate for the "est." chip — more decimals for tiny amounts. */
export function formatEstimateUsd(amount: number): string {
	if (amount === 0) return '$0.00';
	if (amount < 0.01) return `$${amount.toFixed(4)}`;
	return `$${amount.toFixed(2)}`;
}
