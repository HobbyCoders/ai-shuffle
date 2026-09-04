import { updateVoiceContext, type VoiceExecutionContext } from '$lib/api/voice';
import type { ActiveSessionState } from '$lib/stores/active-session';
import { contextWindowForChoice } from '$lib/constants/model-context';
import { getDefaultModel } from '$lib/constants/models';

const writes = new Map<string, { key: string; done: Promise<void> }>();

export function voiceContextFor(state: ActiveSessionState): VoiceExecutionContext {
	const model = state.modelOverride || getDefaultModel();
	return {
		model,
		effort: state.effortLevelOverride || undefined,
		permission_mode: state.permissionModeOverride || 'default',
		context_window: contextWindowForChoice(model, state.contextWindowChoice) || undefined,
		fast: state.fastMode
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
