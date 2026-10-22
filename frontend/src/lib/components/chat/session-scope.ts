/**
 * What a session shows depends on one fact: whether it has a project
 * (issue #371 step 4). There is no chat/code switch any more; these helpers
 * are the one place the composer, the agent card and the attach sheet read
 * that rule from, so they can never disagree.
 *
 * - Access: Ask / Auto / Bypass (+ Away) everywhere; Accept Edits only with a
 *   project, because a session without one has no file-edit tool to approve.
 *   The backend applies the same rule (`session_modes.CHAT_PERMISSION_MODES`).
 * - Runs on: always on the desktop card; on the phone only when the session
 *   would run somewhere other than the worker the phone is connected to.
 * - Attach a project: one-way, only on an idle no-project session this worker
 *   runs, and it puts the session back on Ask.
 */

import type { Install } from '$lib/api/installs';
import { matches } from '$lib/stores/worker-presence';

export interface AccessMode {
	value: 'default' | 'auto' | 'acceptEdits' | 'bypassPermissions';
	label: string;
	description: string;
}

const ASK: AccessMode = { value: 'default', label: 'Ask', description: 'Confirm before tools' };
const AUTO: AccessMode = { value: 'auto', label: 'Auto', description: 'The provider checks each tool call and approves safe ones' };
const ACCEPT_EDITS: AccessMode = { value: 'acceptEdits', label: 'Accept Edits', description: 'File edits run without asking; every other tool asks' };
const BYPASS: AccessMode = { value: 'bypassPermissions', label: 'Bypass', description: 'Execute all tools' };

/** The access modes the composer offers, in menu order. */
export function accessModes(opts: { hasProject: boolean; autoSupported: boolean }): AccessMode[] {
	return [
		ASK,
		...(opts.autoSupported ? [AUTO] : []),
		...(opts.hasProject ? [ACCEPT_EDITS] : []),
		BYPASS
	];
}

/**
 * The mode the composer shows as selected. A stored value the menu does not
 * offer (Accept Edits without a project, Auto on a model without it, the
 * retired plan mode, nothing at all) reads as Ask, which is what the backend
 * runs it as.
 */
export function effectiveAccessMode(
	stored: string | null | undefined,
	opts: { hasProject: boolean; autoSupported: boolean }
): AccessMode['value'] {
	const offered = accessModes(opts);
	return offered.some((mode) => mode.value === stored) ? (stored as AccessMode['value']) : 'default';
}

/**
 * The permission binding a new session without a project starts with, from
 * the owner's last pick. Ask, Auto and Bypass carry over; Accept Edits and
 * the retired plan mode become Ask. `null` (no pick yet) stays `null`, so
 * the agent's own mode decides, as it does for a project session.
 */
export function noProjectPermission(mode: string | null | undefined): string | null {
	if (mode == null) return null;
	return mode === 'default' || mode === 'auto' || mode === 'bypassPermissions' ? mode : 'default';
}

/**
 * Whether the agent card shows its "Runs on" control.
 *
 * Desktop always shows it: it is where a new session's worker is picked. The
 * phone shows it only when the session would run on a worker other than the
 * one the phone is connected to (its top bar already names that one).
 */
export function runsOnVisible(opts: {
	phone: boolean;
	/** Id the new session would run on (install, device or instance id); null/empty = this worker. */
	runsOnId: string | null | undefined;
	/** The install row of the worker this UI is connected to (null when unknown). */
	connected: Install | null | undefined;
}): boolean {
	if (!opts.phone) return true;
	if (!opts.runsOnId) return false;
	// The row's `id` is the device id on a network worker while a pick may name
	// its instance id, so match on every id the row carries.
	return !opts.connected || !matches(opts.connected, opts.runsOnId);
}

export interface AttachState {
	/** The session exists (a new compose picks its project inline instead). */
	sessionId: string | null;
	hasProject: boolean;
	/** Another worker runs the session. */
	isRemote: boolean;
	/** A turn is running or starting. */
	busy: boolean;
	/** A turn is parked on background work (a sub-agent or a background shell). */
	backgroundWork?: boolean;
	archived: boolean;
	/** A subagent's own chat. */
	subagent: boolean;
	/** The legacy assistant home. */
	assistantHome: boolean;
}

/** Whether the "No project" pill shows at all. */
export function noProjectPillVisible(state: AttachState): boolean {
	return !!state.sessionId && !state.hasProject && !state.subagent && !state.assistantHome && !state.archived;
}

/**
 * Why a project cannot be attached right now, or null when it can. The sheet
 * still opens (it explains what attaching does); only its confirm waits.
 */
export function attachBlockedReason(state: AttachState): string | null {
	if (!noProjectPillVisible(state)) return 'This session can’t take a project.';
	if (state.isRemote) return 'Attach the project from the worker that runs this session.';
	if (state.busy) return 'Wait for this turn to finish, then attach.';
	if (state.backgroundWork) return 'Wait for this session’s running or background work to finish, then attach.';
	return null;
}

/**
 * What the attach does to the session's access, shown with the warning.
 * The backend resets Auto and Bypass in the same write that attaches.
 */
export const ATTACH_ACCESS_NOTE = 'Access returns to Ask, so the terminal and file edits ask you first.';

/** The one-way warning the attach sheet shows above its confirm. */
export function attachWarning(agentName: string | null | undefined, projectName: string | null | undefined): {
	title: string;
	body: string;
	access: string;
} {
	const agent = agentName?.trim() || 'Your agent';
	const project = projectName?.trim();
	return {
		title: 'One-way. Projects can’t be detached.',
		body: project
			? `${agent} keeps this conversation and gains ${project}’s checkout, git panel and file tools. To work without a project again, start a new session.`
			: `${agent} keeps this conversation and gains the project’s checkout, git panel and file tools. To work without a project again, start a new session.`,
		access: ATTACH_ACCESS_NOTE
	};
}

/** What appears once a project is attached, for the sheet's preview list. */
export const ATTACH_GAINS: readonly string[] = ['Git panel', 'Branch', 'Checkout', 'Terminal and file edits'];
