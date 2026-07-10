/**
 * Pure gate for whether the chat header's LOCAL git action buttons
 * (Commit / Push / Pull / Create PR / Ship It — and the no-repo "Init Repo"
 * fallback) should render for the active session.
 *
 * Cloud/offloaded sessions run on ANOTHER install: their git repository lives on
 * that owning device, not on the viewer's local filesystem. The header's git
 * controls are driven by the LOCAL `/git/status` poll (git store), so for a
 * remote session that poll finds no repo at the project path and reports
 * `is_git_repo: false` — which makes GitHeaderActions fall back to the misleading
 * "Init Repo" state (and, were a repo present locally, would offer Commit/Push
 * that act on the WRONG local checkout). The synced remote branch context is
 * surfaced separately by the ContextPillBar's read-only "Cloud" badge, so the
 * local git header actions are suppressed entirely for cloud sessions.
 *
 * A remote session is identified by `executionMode === 'cloud'`: the backend
 * `/live` endpoint stamps `execution_mode: "cloud"` on every cross-install
 * session — both when the owner is reachable (relayed live) and when it is
 * offline (served read-only from shared-DB history). See app/api/sessions.py.
 *
 * Extracted from MainPanel so the gating can be unit-tested independently of the
 * Svelte template.
 */
export interface HeaderGitActionsInput {
	/** The active view type ('chat' when a session is open). */
	viewType: string | undefined;
	/** The session has an associated project. */
	hasProject: boolean;
	/** GitHub CLI is authenticated (git controls require it). */
	githubAuthenticated: boolean;
	/** The session's worktree directory is gone (already hides git controls). */
	worktreeDeleted: boolean;
	/** Session execution mode ('local' | 'worktree' | 'cloud'). */
	executionMode: string | undefined;
}

/**
 * Resolve whether the header's local git action buttons should render.
 *
 * Shown only for a chat view with a project on an authenticated, non-deleted,
 * NON-cloud session. Cloud sessions never show local git actions — their repo
 * lives on the owning install, so the local status is meaningless (and would
 * surface a bogus "Init Repo").
 */
export function shouldShowHeaderGitActions(input: HeaderGitActionsInput): boolean {
	const { viewType, hasProject, githubAuthenticated, worktreeDeleted, executionMode } = input;

	if (viewType !== 'chat') return false;
	if (!hasProject) return false;
	if (!githubAuthenticated) return false;
	if (worktreeDeleted) return false;
	// Cloud/offloaded session: repo is on the owning install, never local.
	if (executionMode === 'cloud') return false;

	return true;
}
