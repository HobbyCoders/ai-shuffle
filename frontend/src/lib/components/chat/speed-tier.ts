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

/** How the picked model's Ultrafast is paid for: a ChatGPT plan's allowance or API pricing. */
export type UltrafastBilling = 'plan' | 'api';

/**
 * Inline cost note for the speed picker. Only the phone sheet (`short`)
 * shows a note for ChatGPT plans.
 */
export function ultrafastCost(billing: UltrafastBilling, short = false): string {
	if (billing === 'plan') {
		return short ? 'Ultrafast uses about 8x the plan allowance' : '';
	}
	return short ? 'Ultrafast costs several times the Standard price'
		: 'Ultrafast is the fastest tier. It costs several times the Standard API price.';
}

export const SPEED_OPTIONS: { value: ComposerSpeed; label: string; title: string }[] = [
	{ value: 'standard', label: 'Standard', title: 'Standard: the normal speed and price.' },
	{ value: 'fast', label: 'Fast', title: 'Fast: faster output at a higher price. Switching clears the prompt cache.' },
	{ value: 'ultrafast', label: 'Ultrafast', title: 'Ultrafast: the fastest tier; it uses plan allowance or credits much faster. Switching clears the prompt cache.' }
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
