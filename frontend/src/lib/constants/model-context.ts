import {
	decodeProviderModel,
	getClaudeContextWindow,
	getModelLabel,
	getProviderContextWindow,
	isPresetModel,
	normalizeModel
} from './models';

/** The user's semantic context-window preference, independent of model family. */
export type ContextWindowChoice = 'standard' | '1m';

export const DEFAULT_CONTEXT_WINDOW_CHOICE: ContextWindowChoice = '1m';
export const CLAUDE_STANDARD_CONTEXT = 200_000;
export const GPT_STANDARD_CONTEXT = 256_000;
export const EXTENDED_CONTEXT = 1_000_000;

export interface ModelContextControl {
	standardTokens: typeof CLAUDE_STANDARD_CONTEXT | typeof GPT_STANDARD_CONTEXT;
	extendedTokens: typeof EXTENDED_CONTEXT;
	standardLabel: '200k' | '256k';
	extendedLabel: '1m';
}

/**
 * Return the two context choices for GPT/Claude models that advertise a 1M
 * window in the hydrated registry. Other families and models whose registered
 * ceiling is below 1M do not get a control and never send an override.
 */
export function getModelContextControl(
	model: string | null | undefined
): ModelContextControl | null {
	if (!model) return null;

	const composite = decodeProviderModel(model);
	const modelId = composite?.modelId ?? normalizeModel(model);
	const registeredWindow = composite
		? getProviderContextWindow(composite.modelId, composite.providerId)
		: getClaudeContextWindow(model) ?? getProviderContextWindow(modelId);

	if (registeredWindow == null || registeredWindow < EXTENDED_CONTEXT) return null;

	const identity = `${modelId} ${getModelLabel(model)}`.toLowerCase();
	const nativeClaude = isPresetModel(model);
	if (nativeClaude || identity.includes('claude')) {
		return {
			standardTokens: CLAUDE_STANDARD_CONTEXT,
			extendedTokens: EXTENDED_CONTEXT,
			standardLabel: '200k',
			extendedLabel: '1m'
		};
	}
	if (identity.includes('gpt')) {
		return {
			standardTokens: GPT_STANDARD_CONTEXT,
			extendedTokens: EXTENDED_CONTEXT,
			standardLabel: '256k',
			extendedLabel: '1m'
		};
	}
	return null;
}

/** Concrete request value for a supported model, or null when no override applies. */
export function contextWindowForChoice(
	model: string | null | undefined,
	choice: ContextWindowChoice
): number | null {
	const control = getModelContextControl(model);
	if (!control) return null;
	return choice === '1m' ? control.extendedTokens : control.standardTokens;
}

/** Convert an optional server/session row value back to the semantic picker choice. */
export function contextChoiceFromWindow(
	value: number | null | undefined
): ContextWindowChoice | null {
	if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return null;
	return value >= EXTENDED_CONTEXT ? '1m' : 'standard';
}

export function isContextWindowChoice(value: unknown): value is ContextWindowChoice {
	return value === 'standard' || value === '1m';
}
