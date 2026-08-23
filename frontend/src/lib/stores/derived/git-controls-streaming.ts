/**
 * Pure decision logic for whether the header git controls (Commit / Push / Pull /
 * Create PR / Finish / Ship It + branch selector) should be locked because a
 * session is streaming.
 *
 * Extracted from the `gitControlsStreaming` derived store so the gating — which
 * has several interacting conditions (own-session vs cross-session, worktree vs
 * project mode) — can be unit-tested independently of the Svelte store plumbing.
 * It is the single source of truth the buttons' `disabled` state depends on, so
 * a regression here would silently let Ship It fire mid-stream (sendMessage's
 * interrupt path would abort the live generation).
 */

/** Minimal shape of a cross-device streaming entry (streamingSessions). */
export interface CrossDeviceStreamingInfo {
	projectId: string | null;
	worktreeId: string | null;
	/** The session's logical branch (legacy worktree_branch, or the v2
	 *  sessions.branch a branch-intake session records instead of a legacy
	 *  worktree row). Presence means the session runs in its OWN checkout. */
	worktreeBranch?: string | null;
}

export interface GitControlsStreamingInput {
	/** The active session's own streaming flag — set the instant a send begins.
	 *  Owned by the SSE-fed reducer via activeSession.isStreaming. */
	activeIsStreaming: boolean;
	/** Whether the active session is worktree-mode, derived from its worktree
	 *  fields (worktreeId / worktreeBranch), not a mode flag. */
	activeIsWorktree: boolean;
	/** The active session's project id (empty string / null when none). */
	activeProject: string | null;
	/** Cross-device streaming sessions, keyed by session id (streamingSessions). */
	crossDeviceStreaming: ReadonlyMap<string, CrossDeviceStreamingInfo>;
}

/**
 * Resolve whether git controls should be locked due to streaming.
 *
 * - The active session streaming always locks (any mode) — you can't run a git
 *   action while its own turn is mid-flight.
 * - A streaming session with its own checkout (a legacy worktree row OR a v2
 *   logical branch — branch-intake sessions have worktree_id NULL by design)
 *   never locks OTHER sessions' controls.
 * - Project-mode sessions share the project's main checkout, so any other
 *   main-checkout session (no worktree row, no branch) streaming in the same
 *   project locks them.
 */
export function computeGitControlsStreaming(input: GitControlsStreamingInput): boolean {
	const {
		activeIsStreaming,
		activeIsWorktree,
		activeProject,
		crossDeviceStreaming
	} = input;

	// Active session is streaming (always relevant, regardless of mode).
	if (activeIsStreaming) return true;

	// Worktree-mode has an independent working directory — cross-session
	// streaming in the same project can't touch it, so it doesn't lock controls.
	if (activeIsWorktree) return false;

	// Project-mode: any MAIN-CHECKOUT session streaming in the same project
	// locks the controls (they share the project's main checkout). A streaming
	// session with a worktree row OR a logical branch has its own checkout —
	// v2 branch sessions carry only sessions.branch, worktree_id is NULL.
	if (activeProject) {
		for (const [, info] of crossDeviceStreaming) {
			if (
				info.projectId === activeProject &&
				!info.worktreeId &&
				!info.worktreeBranch
			) {
				return true;
			}
		}
	}

	return false;
}
