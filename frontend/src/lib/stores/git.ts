/**
 * Git store — Re-export shim.
 * Implementation lives in git.svelte.ts (Svelte 5 runes).
 */
export {
	git,
	branches,
	commits,
	worktrees,
	gitStatus,
	diffStats,
	gitLoading,
	gitError,
	gitMode,
	currentBranch,
	localBranches,
	remoteBranches,
} from './git.svelte';

export type {
	Branch,
	CommitNode,
	Worktree,
	GitStatus,
	CreateWorktreeRequest,
	DiffStats,
	PullResponse,
	GitMode,
} from './git.svelte';
