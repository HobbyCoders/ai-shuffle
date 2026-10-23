import { updateVoiceContext, type VoiceExecutionContext } from '$lib/api/voice';
import type { ActiveSessionState } from '$lib/stores/active-session';
import { selectedContextWindow } from '$lib/stores/session/context-window';
import { getDefaultModel } from '$lib/constants/models';
import { isBindingUnhydrated } from '$lib/stores/session/composer-binding';

const writes = new Map<string, { key: string; done: Promise<void> }>();

export function voiceContextFor(state: ActiveSessionState): VoiceExecutionContext {
	const model = state.modelOverride || getDefaultModel();
	return {
		model,
		effort: state.effortLevelOverride || null,
		// An existing session's access and Fast still loading are placeholders
		// (#418): leave the key out so the call keeps the session's own value.
		// `null` is not "unknown" here — it explicitly clears to inherit.
		...(state.sessionId && isBindingUnhydrated(state, 'permission') ? {} : { permission_mode: state.permissionModeOverride || null }),
		context_window: selectedContextWindow(state) ?? null,
		...(state.sessionId && isBindingUnhydrated(state, 'fast') ? {} : { fast: state.fastMode })
	};
}

/** Serialize per owner so a slower earlier PATCH cannot overwrite the latest pick. */
export function persistVoiceContext(owner: string, context: VoiceExecutionContext): Promise<void> {
	const key = JSON.stringify(context);
	const previous = writes.get(owner);
	if (previous?.key === key) return previous.done;
	const done = (previous?.done.catch(() => {}) || Promise.resolve())
		.then(() => updateVoiceContext(owner, context)).then(() => {});
	const entry = { key, done };
	writes.set(owner, entry);
	void done.catch(() => {
		if (writes.get(owner) === entry) writes.delete(owner);
	});
	return done;
}
