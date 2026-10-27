/**
 * What a session shows depends on one fact: whether it has a project
 * (issue #371 step 4). There is no chat/code switch any more; these helpers
 * are the one place the composer, the agent card and the attach sheet read
 * that rule from, so they can never disagree.
 *
 * - Access: Ask / Auto / Accept Edits / Bypass (+ Away) everywhere. The
 *   owner's session without a project is a normal session with the same tools
 *   (#641), so it offers the same modes; the backend runs every mode it is given.
 * - Runs on: always on the desktop card; on the phone only when the session
 *   would run somewhere other than the worker the phone is connected to.
 * - Attach a project: one-way, only on an idle no-project session this worker
 *   runs, and it puts the session back on Ask.
 */


export interface AccessMode {
	value: 'default' | 'auto' | 'acceptEdits' | 'bypassPermissions';
	label: string;
	description: string;
}

const ASK: AccessMode = { value: 'default', label: 'Ask', description: 'Confirm before tools' };
const AUTO: AccessMode = { value: 'auto', label: 'Auto', description: 'The provider checks each tool call and approves safe ones' };
const ACCEPT_EDITS: AccessMode = { value: 'acceptEdits', label: 'Accept Edits', description: 'File edits run without asking; every other tool asks' };
const BYPASS: AccessMode = { value: 'bypassPermissions', label: 'Bypass', description: 'Execute all tools' };

/**
 * The access modes the composer offers, in menu order. `hasProject` no longer
 * changes the list (#641); callers still pass it.
 */
export function accessModes(opts: { hasProject: boolean; autoSupported: boolean }): AccessMode[] {
	return [ASK, ...(opts.autoSupported ? [AUTO] : []), ACCEPT_EDITS, BYPASS];
}

/**
 * The mode the composer shows as selected. A stored value the menu does not
 * offer (Auto on a model without it, the retired plan mode, nothing at all)
 * reads as Ask, which is what the backend runs it as.
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
 * the owner's last pick. Ask, Auto, Accept Edits and Bypass carry over; the
 * retired plan mode (or anything unknown) becomes Ask. `null` (no pick yet)
 * stays `null`, so the agent's own mode decides, as it does for a project session.
 */
export function noProjectPermission(mode: string | null | undefined): string | null {
	if (mode == null) return null;
	const offered = ['default', 'auto', 'acceptEdits', 'bypassPermissions'];
	return offered.includes(mode) ? mode : 'default';
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
	/** A hidden internal chat (the retired assistant home); it never takes a project. */
	hidden: boolean;
}

/** Whether the "No project" pill shows at all. */
export function noProjectPillVisible(state: AttachState): boolean {
	return !!state.sessionId && !state.hasProject && !state.subagent && !state.hidden && !state.archived;
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
		body:
			`${agent} keeps this conversation and moves into ` +
			`${project ? `${project}’s` : 'the project’s'} checkout, with its git panel. ` +
			'To work without a project again, start a new session.',
		access: ATTACH_ACCESS_NOTE
	};
}

/** What appears once a project is attached, for the sheet's preview list. */
export const ATTACH_GAINS: readonly string[] = [
	'Git panel', 'Branch', 'Checkout', 'Terminal in the checkout'
];
