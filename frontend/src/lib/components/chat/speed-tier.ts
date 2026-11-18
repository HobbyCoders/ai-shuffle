/**
 * The composer's speed tier as the controls show it: Standard, Fast and, on
 * routes that carry it, OpenAI's Ultrafast. One place for the names, the cost
 * label and the rule for when the effort label shows a tier.
 */
import type { ComposerSpeed } from '$lib/stores/composer-preferences';

export type { ComposerSpeed };

export const SPEED_NAMES: Record<ComposerSpeed, string> = {
	standard: 'Standard',
	fast: 'Fast',
	ultrafast: 'Ultrafast'
};

/** The cost guard: Ultrafast is offered with its price said up front, never a dialog. */
export const ULTRAFAST_COST =
	'Ultrafast is the fastest tier. It uses plan allowance or credits much faster: about 8x on ChatGPT plans, several times the API price.';

export const SPEED_OPTIONS: { value: ComposerSpeed; label: string; title: string }[] = [
	{ value: 'standard', label: 'Standard', title: 'Standard: the normal speed and price.' },
	{ value: 'fast', label: 'Fast', title: 'Fast: faster output at a higher price. Switching clears the prompt cache.' },
	{ value: 'ultrafast', label: 'Ultrafast', title: `${ULTRAFAST_COST} Switching clears the prompt cache.` }
];

export interface SpeedShownInput {
	fastSupported?: boolean;
	fastEnabled?: boolean;
	fastPending?: boolean;
	ultrafastSupported?: boolean;
	ultrafastEnabled?: boolean;
}

/**
 * The tier the chat runs on for the picked model, for the effort label.
 * Standard while the chat's own setting is still loading or the model lacks
 * the tier, so the label never claims a speed the next turn will not ask for.
 */
export function shownSpeed(s: SpeedShownInput): ComposerSpeed {
	if (!s.fastSupported || !s.fastEnabled || s.fastPending) return 'standard';
	return s.ultrafastSupported && s.ultrafastEnabled ? 'ultrafast' : 'fast';
}

/** The tier the picker selects (the chat's own setting, shown or not). */
export function pickedSpeed(s: SpeedShownInput): ComposerSpeed {
	if (!s.fastEnabled) return 'standard';
	return s.ultrafastSupported && s.ultrafastEnabled ? 'ultrafast' : 'fast';
}

/** "Effort: High", "Effort: High, Ultrafast". */
export function effortSpeedLabel(prefix: string, effort: string, speed: ComposerSpeed): string {
	return speed === 'standard' ? `${prefix}${effort}` : `${prefix}${effort}, ${SPEED_NAMES[speed]}`;
}
