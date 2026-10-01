import {
	decodeProviderModel,
	getClaudeContextWindow,
	getModelLabel,
	getProviderContextWindow,
	getProviders,
	isPresetModel,
	normalizeModel
} from './models';

/** The user's semantic context-window preference, independent of model family. */
export type ContextWindowChoice = 'standard' | '1m';

export const DEFAULT_CONTEXT_WINDOW_CHOICE: ContextWindowChoice = '1m';
const CLAUDE_STANDARD_CONTEXT = 200_000;
const GPT_STANDARD_CONTEXT = 256_000;
const GPT_272K_CONTEXT = 272_000;
const EXTENDED_CONTEXT = 1_000_000;

export interface ModelContextControl {
	standardTokens: typeof CLAUDE_STANDARD_CONTEXT | typeof GPT_STANDARD_CONTEXT | typeof GPT_272K_CONTEXT;
	extendedTokens: typeof EXTENDED_CONTEXT;
	standardLabel: '200K' | '256K' | '272K';
	extendedLabel: '1M';
}

/**
 * Use the registry's selectable windows, which can exceed a model's default.
 * Older registries without explicit choices fall back to the advertised window.
 */
export function getModelContextControl(
	model: string | null | undefined
): ModelContextControl | null {
	if (!model) return null;

	const composite = decodeProviderModel(model);
	const modelId = composite?.modelId ?? normalizeModel(model);
	const nativeClaude = isPresetModel(model);
	const choices = nativeClaude
		? undefined
		: getProviders()
				.filter((provider) => !composite || provider.id === composite.providerId)
				.flatMap((provider) => provider.models)
				.find((entry) => entry.id === modelId)?.context_window_choices;
	const registeredWindow = composite
		? getProviderContextWindow(composite.modelId, composite.providerId)
		: getClaudeContextWindow(model) ?? getProviderContextWindow(modelId);

	if (choices !== undefined) {
		if (!choices.includes(EXTENDED_CONTEXT)) return null;
	} else if (registeredWindow == null || registeredWindow < EXTENDED_CONTEXT) return null;

	const identity = `${modelId} ${getModelLabel(model)}`.toLowerCase();
	if (nativeClaude || identity.includes('claude')) {
		if (choices && !choices.includes(CLAUDE_STANDARD_CONTEXT)) return null;
		return {
			standardTokens: CLAUDE_STANDARD_CONTEXT,
			extendedTokens: EXTENDED_CONTEXT,
			standardLabel: '200K',
			extendedLabel: '1M'
		};
	}
	if (identity.includes('gpt')) {
		const standard = choices?.includes(GPT_272K_CONTEXT) ? GPT_272K_CONTEXT : GPT_STANDARD_CONTEXT;
		if (choices && !choices.includes(standard)) return null;
		return {
			standardTokens: standard,
			extendedTokens: EXTENDED_CONTEXT,
			standardLabel: standard === GPT_272K_CONTEXT ? '272K' : '256K',
			extendedLabel: '1M'
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
