/**
 * startNewVoiceSession — create a voice session and put the user straight into
 * the call.
 *
 * This sequence used to live inside VoiceCallButton, back when the composer was
 * the only place a voice session could be born. Voice is now its own sidepanel
 * lane with its own new-session screen, so two surfaces offer the same action
 * and the ordering below has to hold for both — duplicating it is exactly how
 * that ordering gets quietly broken.
 */
import { get } from 'svelte/store';
import { activeSession } from '$lib/stores/active-session';
import { appData } from '$lib/stores/app-data.svelte';
import { layout } from '$lib/stores/layout.svelte';
import { appMode } from '$lib/stores/sessionMode.svelte';
import { notify } from '$lib/stores/notifications';
import { createVoiceSession } from '$lib/api/voice';
import { startCall, confirmCallReplacement } from '$lib/services/voiceCall';
import { contextWindowForChoice } from '$lib/constants/model-context';
import { getDefaultModel } from '$lib/constants/models';

// Both the composer's call button and the voice lane's hero call this. A second
// creation while the first is still handshaking would strand an unused session
// on the board and race two calls onto the same mic.
let starting = false;

/** Create a voice session, switch to it, and auto-start the call.
 *  Returns true on success; surfaces its own error toast and returns false otherwise. */
export async function startNewVoiceSession(): Promise<boolean> {
	if (starting) return false;
	starting = true;
	const from = get(activeSession);
	try {
		if (!(await confirmCallReplacement(null))) return false;
		const model = from.modelOverride || getDefaultModel();
		const effort = from.effortLevelOverride;
		const created = await createVoiceSession({
			profile_id: from.profile || null,
			project_id: from.project || null,
			project_v2_id: from.projectV2Id,
			worktree_id: from.worktreeId,
			worktree_branch: from.selectedWorktreeBranch || from.worktreeBranch || null,
			base_branch: from.worktreeBaseBranch || null,
			execution_mode: from.worktreeIntent,
			target_instance_id: from.deviceIntent?.id || null,
			model,
			permission_mode: from.permissionModeOverride,
			effort,
			fast: from.fastMode,
			context_window: contextWindowForChoice(model, from.contextWindowChoice)
		});
		// The board row must exist before we switch: ChatView reads `mode`
		// off the sessions list to decide the surface.
		await appData.loadSessions();
		// Set the lane before the switch so the sidepanel is already listing
		// voice sessions when the new one becomes active — otherwise the row we
		// just opened is filtered out of the list behind it.
		appMode.set('voice');
		activeSession.switchSession(
			created.session_id,
			from.project || null,
			from.worktreeId || from.worktreeBranch ? { worktreeId: from.worktreeId, worktreeBranch: from.selectedWorktreeBranch || from.worktreeBranch, worktreeIntent: from.worktreeIntent, worktreeDeleted: false } : null,
			'Voice session',
			from.projectV2Id
		);
		layout.openChat(created.session_id);
		// Auto-start — the affordance IS the call (the channel is always open
		// once live; there is no separate "connect" step for the user).
		await startCall(created.session_id, {
			client_secret: created.client_secret,
			model: created.model,
			options: created.options
		});
		return true;
	} catch (e) {
		// The backend's 400/403 detail strings are written for the user
		// (missing/ineligible provider, voice mode disabled) — show them as-is.
		const detail = (e as { detail?: string } | null)?.detail;
		notify.error(detail || 'Could not start a voice session.');
		return false;
	} finally {
		starting = false;
	}
}
