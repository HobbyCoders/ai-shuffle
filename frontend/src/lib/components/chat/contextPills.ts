/**
 * Pure decision logic for the ContextPillBar's read-only context badge.
 *
 * The badge is the project/worktree indicator that used to live in the page
 * header. It moved into the pill bar above the chat input, where it fills in
 * for the interactive BranchSelector pill when GitHub CLI isn't authenticated
 * (no branch controls exist at all) but the session still has project/worktree
 * context worth surfacing. (Worktree sessions whose folder was cleaned up still
 * show the normal — read-only-in-worktree-mode — branch pill, not a badge.)
 *
 * Extracted so the gating — which has several interacting conditions — can be
 * unit-tested independently of the Svelte component.
 */

export interface ContextBadgeInput {
	/** Number of messages in the active session (0 ⇒ empty/new-session state). */
	messageCount: number;
	/** Display name of the session's project, or null when unknown. */
	projectName: string | null;
	/** Whether the session is worktree-mode, derived from its worktree fields
	 *  (worktreeId / worktreeBranch), not a mode flag. */
	isWorktree: boolean;
	/** Branch chosen for an existing worktree, if any. */
	selectedWorktreeBranch: string | null;
	/** Branch the worktree was/will be created on. */
	worktreeBranch: string | null;
	/** Whether GitHub CLI is authenticated (gates the interactive branch pill). */
	githubAuthenticated: boolean;
	/**
	 * V2 Q7: the active session has a queued turn but no capable runner online
	 * (server-derived). When true, a read-only "no runner online" badge takes
	 * precedence over the normal project/branch context.
	 */
	degraded?: boolean;
	/** V2 Q8: the session is owned by (placed on) another install (is_remote). */
	isRemote?: boolean;
	/** V2 Q8: friendly name of the owning device (only meaningful when isRemote). */
	deviceName?: string | null;
	/** V2 Q8: whether the owning device is currently online (only when isRemote). */
	deviceOnline?: boolean;
}

export interface ContextBadge {
	/** Worktree branch to show, or null to show only the project. */
	branch: string | null;
	/** Tooltip text. */
	title: string;
	/**
	 * V2 Q8: present for a remote-placed session — renders a device indicator
	 * (which install the session runs on, and whether it is online) instead of a
	 * local branch.
	 */
	runner?: { deviceName: string; online: boolean };
	/** V2 Q7: true for the read-only "no runner online" degraded badge. */
	degraded?: boolean;
}

/**
 * Resolve the read-only context badge for the pill bar, or null when none
 * applies. Mirrors (and is the single source of truth for) the gating the
 * ContextPillBar uses to choose between the interactive branch pill and a
 * static badge.
 */
export function computeContextBadge(input: ContextBadgeInput): ContextBadge | null {
	const {
		messageCount,
		projectName,
		isWorktree,
		selectedWorktreeBranch,
		worktreeBranch,
		githubAuthenticated,
		degraded,
		isRemote,
		deviceName,
		deviceOnline
	} = input;

	// The empty/new-session state is owned by the execution-mode toggle, not a
	// context badge — so a badge only ever applies once a chat is active.
	if (messageCount === 0) return null;

	// V2 Q7: a degraded session (queued work, no runner online) is read-only.
	// Surface that as its own badge with top precedence over any project/branch
	// context, regardless of GitHub-auth gating. It only applies once a chat is
	// active (above) — the empty/new-session compose case is covered by the board
	// chip, so the badge stays in-chat only.
	if (degraded) {
		return {
			branch: null,
			degraded: true,
			title: 'Read-only — no runner online for this session'
		};
	}

	// V2 Q8: a remote-placed session runs on another install. Surface which
	// device it runs on (and whether that device is online) as a read-only badge
	// in place of local branch context — the viewer's local checkout isn't where
	// the work runs. Derived from the session's ownership fields, never from the
	// execution mode.
	if (isRemote) {
		const name = deviceName || 'another device';
		const prefix = projectName ? `${projectName} · ` : '';
		return {
			branch: null,
			runner: { deviceName: name, online: !!deviceOnline },
			title: `${prefix}Running on ${name}${deviceOnline ? '' : ' (offline)'}`
		};
	}

	// The session's worktree branch (only meaningful in worktree mode).
	const branch = isWorktree ? selectedWorktreeBranch || worktreeBranch || null : null;

	// When GitHub CLI is authenticated the interactive BranchSelector pill is
	// shown instead — never both. This now includes cleaned-up worktree sessions:
	// they show the normal (read-only-in-worktree-mode) branch pill, not a badge.
	if (githubAuthenticated) return null;

	// Unauthenticated: surface read-only project/branch context, when present.
	const applies = !!projectName || !!branch;
	if (!applies) return null;

	const prefix = projectName ? `${projectName} · ` : '';
	const title = branch ? `${prefix}Worktree branch: ${branch}` : `Project: ${projectName}`;

	return { branch, title };
}

export interface EmptyStateBranchInput {
	/** Number of messages in the active session (0 ⇒ empty/new-session state). */
	messageCount: number;
	/** The session's project id, or null when none is selected. */
	project: string | null;
	/** Whether GitHub CLI is authenticated (gates the branch controls). */
	githubAuthenticated: boolean;
	/** Compose-time Local/Worktree toggle intent ('local' | 'worktree'). */
	worktreeIntent: string;
	/**
	 * Whether the selected project folder is a git repository. The branch pill
	 * only makes sense for a repo; a non-git folder has no branches to pick.
	 * Defaults to true so the (brief) pre-load state still shows the pill.
	 */
	isGitRepo?: boolean;
}

/**
 * Whether the interactive BranchSelector pill should appear on the new-session
 * screen (next to the profile + execution-mode pills).
 *
 * Shown only in LOCAL mode: in worktree mode the ExecutionModeBar hosts its own
 * branch picker (which branch to base the new worktree on), so a second pill
 * would double up on the same row. In local mode the ExecutionModeBar has no
 * branch control at all, so without this pill there'd be no way to see or change
 * the working branch before sending the first message.
 *
 * Gated on project + GitHub auth to mirror the active-chat branch-pill (and the
 * ExecutionModeBar's) gate, so the new-session and active-chat states agree on
 * when branch controls are available.
 */
export function shouldShowEmptyStateBranchPill(input: EmptyStateBranchInput): boolean {
	const { messageCount, project, githubAuthenticated, worktreeIntent, isGitRepo = true } = input;

	// New-session screen only — once a chat is active the branch pill / context
	// badge (computeContextBadge) owns this slot.
	if (messageCount !== 0) return false;
	if (!project || !githubAuthenticated) return false;
	// A non-git project has no branches to pick — hide the branch pill entirely.
	if (!isGitRepo) return false;
	// Only local mode has a working branch to pick here. Worktree mode's branch
	// picker lives in the ExecutionModeBar, so it doesn't show the local branch pill.
	if (worktreeIntent !== 'local') return false;
	return true;
}
