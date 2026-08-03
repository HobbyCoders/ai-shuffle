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
 * - Worktree-mode sessions have an independent working directory, so streaming
 *   in *other* sessions of the same project doesn't lock them.
 * - Project-mode sessions share the project's main checkout, so any other
 *   non-worktree session streaming in the same project locks them.
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

	// Project-mode: any non-worktree session streaming in the same project locks
	// the controls (they share the project's main checkout).
	if (activeProject) {
		for (const [, info] of crossDeviceStreaming) {
			if (info.projectId === activeProject && !info.worktreeId) return true;
		}
	}

	return false;
}
