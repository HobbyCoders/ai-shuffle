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
	 * Code and voice keep it on both shells — including the mobile shell, where
	 * Finish must remain reachable when a PR is open. Omitted values fail open.
	 */
	sessionMode?: string;
}

/** Session-scoped Git requests execute on the owning worker, local or remote. */
export function shouldShowHeaderGitActions(input: HeaderGitActionsInput): boolean {
	const { viewType, hasProject, worktreeDeleted, sessionMode } = input;

	if (viewType !== 'session') return false;
	if (!hasProject) return false;
	if (worktreeDeleted) return false;
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
