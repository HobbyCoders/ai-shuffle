/**
 * Who may move a chat to another worker (the runner chip's Move menu).
 *
 * One rule for every layout: the desktop context row and the phone's Chat
 * controls render the same ContextPillBar, which reads this to choose between
 * the Move menu and a read-only "Runs on" chip. The move itself, its busy
 * rule and its refusal messages stay in that component and the move API.
 *
 * A chat can move when:
 *   - it is saved (it has a durable id and a sessions-board row; a new chat
 *     picks its worker in the composer instead),
 *   - it is not a subagent's own chat (read and message only),
 *   - it was not started with an API key (`api_user_id`): those run under the
 *     key's restricted identity, which the owner's workers do not hand over,
 *   - the viewer is the owner. A paired device of another account member, or
 *     an API-key login, sees where the chat runs but cannot move it. Before
 *     the auth status has loaded the viewer is not known to be someone else,
 *     so the control stays; the move endpoint is still the authority.
 */
export interface SessionMoveInput {
	sessionId: string | null | undefined;
	/** The chat's sessions-board row, or null before it arrives. */
	row: { api_user_id?: string | null } | null | undefined;
	isSubagent: boolean;
	/** Signed-in status, from the auth store; false while it loads. */
	viewerAuthenticated: boolean;
	viewerIsAdmin: boolean;
}

/** True when the viewer is someone other than this installation's owner. */
export function viewerIsNonOwner(authenticated: boolean, isAdmin: boolean): boolean {
	return authenticated && !isAdmin;
}

export function canMoveSession(input: SessionMoveInput): boolean {
	if (!input.sessionId || !input.row) return false;
	if (input.isSubagent) return false;
	if (input.row.api_user_id) return false;
	return !viewerIsNonOwner(input.viewerAuthenticated, input.viewerIsAdmin);
}
