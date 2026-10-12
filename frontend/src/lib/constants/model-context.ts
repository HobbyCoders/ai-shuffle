import {
	decodeProviderModel,
	getClaudeContextWindow,
	getModelLabel,
	getProviderContextWindow,
	getProviders,
	isPresetModel,
	normalizeModel
} from './models';

/**
 * The user's semantic context-window preference, independent of model family.
 * `standard` is a cap the user can raise (from the family default up to just
 * below the full window); `1m` is Extended, locked to the model's full window.
 */
export type ContextWindowChoice = 'standard' | '1m';

export const DEFAULT_CONTEXT_WINDOW_CHOICE: ContextWindowChoice = '1m';
const CLAUDE_STANDARD_CONTEXT = 200_000;
const GPT_STANDARD_CONTEXT = 256_000;
const GPT_272K_CONTEXT = 272_000;
/** The opt-in long-context window that makes a model's context adjustable. */
const EXTENDED_CONTEXT = 1_000_000;

/** Smallest unit the Standard cap is edited in: whole K tokens. */
export const CONTEXT_CAP_UNIT = 1_000;
/** Grid the slider and arrow keys snap to. */
export const CONTEXT_CAP_STEP = 10_000;

export interface ModelContextControl {
	/** The family's default Standard window, and the floor of the cap. */
	standardTokens: number;
	/** The highest Standard cap: one slider step below Extended. */
	standardMaxTokens: number;
	/** The model's full window. Extended always runs at this; never editable. */
	extendedTokens: number;
	standardLabel: string;
	extendedLabel: string;
}

/** `200K`, `272K`, `1M`, `1.5M` — compact K/M labels for token counts. */
export function formatContextTokens(tokens: number): string {
	if (tokens >= 1_000_000) {
		const millions = tokens / 1_000_000;
		return `${Number.isInteger(millions) ? millions : Number(millions.toFixed(2))}M`;
	}
	return `${Math.round(tokens / CONTEXT_CAP_UNIT)}K`;
}

function control(standard: number, extended: number): ModelContextControl {
	return {
		standardTokens: standard,
		standardMaxTokens: Math.max(standard, extended - CONTEXT_CAP_STEP),
		extendedTokens: extended,
		standardLabel: formatContextTokens(standard),
		extendedLabel: formatContextTokens(extended)
	};
}

/**
 * Use the registry's selectable windows (`[standard, full]`), which can exceed
 * a model's default. Older registries without explicit choices fall back to
 * the advertised window.
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

	let extended: number;
	if (choices !== undefined) {
		if (choices.length === 0) return null;
		extended = Math.max(...choices);
		if (extended < EXTENDED_CONTEXT) return null;
	} else {
		if (registeredWindow == null || registeredWindow < EXTENDED_CONTEXT) return null;
		extended = registeredWindow;
	}

	const identity = `${modelId} ${getModelLabel(model)}`.toLowerCase();
	if (nativeClaude || identity.includes('claude')) {
		if (choices && !choices.includes(CLAUDE_STANDARD_CONTEXT)) return null;
		return control(CLAUDE_STANDARD_CONTEXT, extended);
	}
	if (identity.includes('gpt')) {
		const standard = choices?.includes(GPT_272K_CONTEXT) ? GPT_272K_CONTEXT : GPT_STANDARD_CONTEXT;
		if (choices && !choices.includes(standard)) return null;
		return control(standard, extended);
	}
	return null;
}

/** A Standard cap fitted into the model's range, in whole K tokens. */
export function clampStandardCap(controlFor: ModelContextControl, tokens: number): number {
	if (!Number.isFinite(tokens)) return controlFor.standardTokens;
	const whole = Math.round(tokens / CONTEXT_CAP_UNIT) * CONTEXT_CAP_UNIT;
	return Math.min(controlFor.standardMaxTokens, Math.max(controlFor.standardTokens, whole));
}

/** Quick picks for the Standard cap: the default, then round steps that fit. */
export function standardCapPresets(controlFor: ModelContextControl): number[] {
	const steps = [400_000, 600_000, 800_000, 1_200_000, 1_600_000, 2_000_000];
	return [
		controlFor.standardTokens,
		...steps
			.filter((value) => value > controlFor.standardTokens && value <= controlFor.standardMaxTokens)
			.slice(0, 3)
	];
}

/**
 * Concrete request value for a supported model, or null when no override
 * applies. `standardCap` is the raised Standard cap in effect (the chat's saved
 * pin or the owner's per-model default); absent, Standard is the family default.
 */
export function contextWindowForChoice(
	model: string | null | undefined,
	choice: ContextWindowChoice,
	standardCap?: number | null
): number | null {
	const controlFor = getModelContextControl(model);
	if (!controlFor) return null;
	if (choice === '1m') return controlFor.extendedTokens;
	return typeof standardCap === 'number' && standardCap > 0
		? clampStandardCap(controlFor, standardCap)
		: controlFor.standardTokens;
}

/**
 * Convert an optional server/session row value back to the semantic picker
 * choice. With the chat's model known, only its full window reads as Extended;
 * without it, the 1M opt-in threshold decides.
 */
export function contextChoiceFromWindow(
	value: number | null | undefined,
	model?: string | null
): ContextWindowChoice | null {
	if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return null;
	const extended = (model ? getModelContextControl(model)?.extendedTokens : null) ?? EXTENDED_CONTEXT;
	return value >= extended ? '1m' : 'standard';
}

export function isContextWindowChoice(value: unknown): value is ContextWindowChoice {
	return value === 'standard' || value === '1m';
}
