/**
 * The context window a composer runs at: the semantic Standard/Extended choice
 * plus the Standard cap in effect. Plain .ts (rune-free) so stores and services
 * can share it.
 *
 * The cap comes from, in order:
 *   1. the open chat's saved pin when it is a Standard value for this model
 *      (the chat owns its own window, like its effort);
 *   2. the owner's per-model default (`composerPreferences.contextCapForModel`);
 *   3. nothing — Standard runs at the model family's default.
 */
import { getDefaultModel } from '$lib/constants/models';
import {
	clampStandardCap,
	contextWindowForChoice,
	getModelContextControl,
	type ContextWindowChoice
} from '$lib/constants/model-context';
import { composerPreferences } from '../composer-preferences';

export interface ContextWindowState {
	sessionId: string | null;
	modelOverride: string | null;
	contextWindowChoice: ContextWindowChoice;
	contextWindowPin?: number | null;
}

/** The Standard cap (tokens) this composer shows and sends, or null without a context control. */
export function standardContextCap(state: ContextWindowState): number | null {
	const model = state.modelOverride || getDefaultModel();
	const control = getModelContextControl(model);
	if (!control) return null;
	const pin = state.contextWindowPin;
	if (state.sessionId && typeof pin === 'number' && pin > 0 && pin < control.extendedTokens) {
		return clampStandardCap(control, pin);
	}
	const saved = composerPreferences.contextCapForModel(model);
	return saved != null ? clampStandardCap(control, saved) : control.standardTokens;
}

/** The numeric window for the composer's current pick, or null when the model has no choice. */
export function selectedContextWindow(state: ContextWindowState): number | null {
	return contextWindowForChoice(
		state.modelOverride || getDefaultModel(),
		state.contextWindowChoice,
		standardContextCap(state)
	);
}
