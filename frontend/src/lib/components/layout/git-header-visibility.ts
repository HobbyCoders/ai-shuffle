/**
 * Pure gates for the chat header's git controls.
 *
 * Two questions, deliberately separate (finding G10c):
 *
 * 1. `shouldShowHeaderGitActions` — may the LOCAL git controls (Commit / Push /
 *    Pull, the branch pill, and the no-repo "Init Repo" fallback) render?
 *    Local git needs a local checkout and nothing else. It does **not** need
 *    GitHub authentication: `githubAuthenticated` comes from a 120 s
 *    `/auth/status` poll whose `gh auth status` subprocess transiently fails
 *    under load, and gating on it hid the entire git UI for up to two minutes
 *    on a blip.
 *
 * 2. `shouldShowGithubGitActions` — may the GITHUB-backed controls (Create PR /
 *    Finish / Ship It) render? Those genuinely need `gh`, so they consult the
 *    hysteresis-guarded availability flag from the git store
 *    (`githubFeaturesAvailable`), not the raw poll result.
 *
 * A remote-placed session runs on ANOTHER install: its git repository lives on
 * that owning device, not on the viewer's local filesystem. The header's git
 * controls are driven by the LOCAL `/git/status` poll (git store), so for a
 * remote session that poll finds no repo at the project path and reports
 * `is_git_repo: false` — which makes GitHeaderActions fall back to the misleading
 * "Init Repo" state (and, were a repo present locally, would offer Commit/Push
 * that act on the WRONG local checkout). The synced remote branch context is
 * surfaced separately by the ContextPillBar's read-only runner badge, so the
 * local git header actions are suppressed entirely for remote-placed sessions.
 *
 * A remote session is identified by its ownership field `is_remote`: the backend
 * annotates every cross-install session as owned by another install — both when
 * the owner is reachable and when it is offline (served read-only from shared-DB
 * history). See app/api/sessions.py.
 *
 * Extracted from MainPanel so the gating can be unit-tested independently of the
 * Svelte template.
 */
export interface HeaderGitActionsInput {
	/** The active view type ('session' when the conversation panel is open). */
	viewType: string | undefined;
	/** The session has an associated project. */
	hasProject: boolean;
	/**
	 * Raw GitHub CLI authentication verdict.
	 *
	 * IGNORED by `shouldShowHeaderGitActions` — local git works without GitHub.
	 * Kept on the input so existing callers keep type-checking, and consumed by
	 * `shouldShowGithubGitActions` (which prefers `githubAvailable` when the
	 * caller supplies the debounced store flag).
	 */
	githubAuthenticated?: boolean;
	/**
	 * The git store's hysteresis-guarded "GitHub features are usable" flag
	 * (`githubFeaturesAvailable`). Preferred over the raw poll verdict because a
	 * single failed `gh auth status` never flips it. Defaults to
	 * `githubAuthenticated` when omitted.
	 */
	githubAvailable?: boolean;
	/** The session's worktree directory is gone (already hides git controls). */
	worktreeDeleted: boolean;
	/** The session is owned by (placed on) another install. */
	isRemote: boolean;
	/**
	 * Session's own lane ('code' | 'chat' | 'voice').
	 * Chat-lane sessions have no repository; hide the git rail for them.
	 * Code and voice keep it on every width — including mobile, where Finish
	 * must remain reachable when a PR is open. Omitted values fail open.
	 */
	sessionMode?: string;
}

/**
 * Resolve whether the header's LOCAL git action buttons should render.
 *
 * Shown for a session view with a project on a non-deleted, NON-remote session.
 * Remote-placed sessions never show local git actions — their repo lives on the
 * owning install, so the local status is meaningless (and would surface a bogus
 * "Init Repo"). Chat-lane sessions have no repo, so the rail is hidden for
 * them. Code (and voice) keep Commit/Push/Finish on desktop AND mobile —
 * never gate on viewport width.
 *
 * Deliberately independent of GitHub auth: a `gh` blip must never blank the
 * local git UI (G10c).
 */
export function shouldShowHeaderGitActions(input: HeaderGitActionsInput): boolean {
	const { viewType, hasProject, worktreeDeleted, isRemote, sessionMode } = input;

	if (viewType !== 'session') return false;
	if (!hasProject) return false;
	if (worktreeDeleted) return false;
	// Remote-placed session: repo is on the owning install, never local.
	if (isRemote) return false;
	// Chat lane has no repository. Do NOT also require isMobile: that blanked
	// Finish/Commit/Push on mobile *code* sessions (the conversation view is
	// still viewType 'session' on every lane).
	if (sessionMode === 'chat') return false;

	return true;
}

/**
 * Resolve whether the header's GITHUB-backed actions (Create PR / Finish /
 * Ship It) should render. Requires everything the local gate requires, plus
 * usable GitHub access.
 */
export function shouldShowGithubGitActions(input: HeaderGitActionsInput): boolean {
	if (!shouldShowHeaderGitActions(input)) return false;
	return input.githubAvailable ?? input.githubAuthenticated ?? false;
}
