import { updateVoiceContext, type VoiceExecutionContext } from '$lib/api/voice';
import type { ActiveSessionState } from '$lib/stores/active-session';
import { selectedContextWindow } from '$lib/stores/session/context-window';
import { getDefaultModel } from '$lib/constants/models';
import { isBindingUnhydrated } from '$lib/stores/session/composer-binding';
import { fittedSendEffort } from '$lib/constants/model-effort';
import { effortPickToSend, effortRevToSend } from '$lib/stores/session/effort-revision';

const writes = new Map<string, { key: string; done: Promise<VoiceExecutionContext> }>();

/**
 * The worker context a call should use, as the composer shows it. A snapshot,
 * not a pick (picks are saved by the run-settings endpoint): its level rides
 * with the revision it was saved at, so a level from an outdated view never
 * replaces a newer one (#1609), and is left out when that revision is unknown.
 * `row` is the chat's sidebar row, which names the revision of a chat created
 * in this tab.
 */

export function voiceContextFor(state: ActiveSessionState, row?: { effort?: string | null; effort_rev?: number; effort_pick?: string | null }): VoiceExecutionContext {
	const model = state.modelOverride || getDefaultModel();
	const effortRev = state.sessionId ? effortRevToSend(state, row) : undefined;
	return {
		model,
		...(effortRev === undefined ? {} : { effort: fittedSendEffort(state), effort_rev: effortRev, effort_pick: effortPickToSend(state, row) }),
		// An existing session's access and Fast still loading are placeholders
		// (#418): leave the key out so the call keeps the session's own value.
		// `null` is not "unknown" here — it explicitly clears to inherit.
		...(state.sessionId && isBindingUnhydrated(state, 'permission') ? {} : { permission_mode: state.permissionModeOverride || null }),
		context_window: selectedContextWindow(state) ?? null,
		...(state.sessionId && isBindingUnhydrated(state, 'fast') ? {} : { fast: state.fastMode })
	};
}

/** A render or model switch is not an effort pick. Only a delegation send
 * includes the fitted level; an explicit pick already has its own PUT. */
export function idleVoiceContextFor(state: ActiveSessionState): VoiceExecutionContext {
	const { effort, effort_rev, effort_pick, ...context } = voiceContextFor(state);
	return context;
}

/** Serialize per owner so a slower earlier PATCH cannot overwrite the latest pick. */
export function persistVoiceContext(owner: string, context: VoiceExecutionContext): Promise<VoiceExecutionContext> {
	const key = JSON.stringify(context);
	const previous = writes.get(owner);
	if (!('effort' in context) && previous?.key === key) return previous.done;
	const done = (previous?.done.catch(() => {}) || Promise.resolve())
		.then(() => updateVoiceContext(owner, context));
	const entry = { key, done };
	writes.set(owner, entry);
	void done.catch(() => {
		if (writes.get(owner) === entry) writes.delete(owner);
	});
	return done;
}
