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
	/** Session execution mode ('local' | 'worktree'). */
	executionMode: string;
	/** Branch chosen for an existing worktree, if any. */
	selectedWorktreeBranch: string | null;
	/** Branch the worktree was/will be created on. */
	worktreeBranch: string | null;
	/** Whether GitHub CLI is authenticated (gates the interactive branch pill). */
	githubAuthenticated: boolean;
}

export interface ContextBadge {
	/** Worktree branch to show, or null to show only the project. */
	branch: string | null;
	/** Tooltip text. */
	title: string;
	/** True for cloud sessions (renders a cloud indicator instead of a branch). */
	cloud?: boolean;
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
		executionMode,
		selectedWorktreeBranch,
		worktreeBranch,
		githubAuthenticated
	} = input;

	// The empty/new-session state is owned by the execution-mode toggle, not a
	// context badge — so a badge only ever applies once a chat is active.
	if (messageCount === 0) return null;

	// Cloud sessions always show a read-only "cloud" badge so it's clear the
	// work is running on the configured second install (there's no local branch).
	if (executionMode === 'cloud') {
		const prefix = projectName ? `${projectName} · ` : '';
		return {
			branch: null,
			cloud: true,
			title: `${prefix}Running on the cloud install`
		};
	}

	// The session's worktree branch (only meaningful in worktree mode).
	const branch =
		executionMode === 'worktree' ? selectedWorktreeBranch || worktreeBranch || null : null;

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
	/** Session execution mode ('local' | 'worktree'). */
	executionMode: string;
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
	const { messageCount, project, githubAuthenticated, executionMode, isGitRepo = true } = input;

	// New-session screen only — once a chat is active the branch pill / context
	// badge (computeContextBadge) owns this slot.
	if (messageCount !== 0) return false;
	if (!project || !githubAuthenticated) return false;
	// A non-git project has no branches to pick — hide the branch pill entirely.
	if (!isGitRepo) return false;
	// Only local mode has a working branch to pick here. Worktree mode's branch
	// picker lives in the ExecutionModeBar, and cloud mode lets the worker pick
	// the branch — so neither shows the local branch pill.
	if (executionMode !== 'local') return false;
	return true;
}
