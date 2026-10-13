/**
 * Pure decision logic for whether the header "Ship It" button should be shown.
 *
 * Ship It runs the whole pipeline (commit → push → PR → merge → cleanup), so it
 * must only appear when the folder is a confirmed git repository AND the branch
 * has uncommitted changes, unmerged branch content, or an open PR to merge. The repo-confirmation guard matters
 * because worktree sessions are reusable — the same folder can be emptied/cleaned
 * up between sessions, and Ship It must never render against a folder with no repo.
 *
 * Extracted from GitHeaderActions so the gating can be unit-tested independently
 * of the Svelte template. The previous inline condition keyed off the raw
 * commit-count-ahead (`gitStatus.ahead`), which left Ship It visible for branches
 * that sit "ahead" by commit count yet carry a 0/0 net diff — empty or cancelling
 * commits, or a cleaned-up worktree branch already merged into the base. Those
 * branches have nothing to ship, so the button must be hidden.
 */
export interface ShipItVisibilityInput {
	/** The working folder is a *confirmed* git repository — status has loaded and
	 *  reports `is_git_repo === true`. Deliberately strict (not "not false"): an
	 *  empty or cleaned-up worktree folder whose status is null/unconfirmed or
	 *  reports `is_git_repo: false` must never show Ship It. Worktree sessions are
	 *  reusable, so the same folder can be emptied between sessions — Ship It must
	 *  track whether the folder actually has a repo, not assume it optimistically. */
	isRepoConfirmed: boolean;
	/** Uncommitted working-tree changes (staged/unstaged additions or deletions). */
	hasChanges: boolean;
	/** Current HEAD is already included in a merged PR. */
	isHeadMerged?: boolean;
	/** An existing PR still needs to be merged, even when the branch diff is empty. */
	hasOpenPr?: boolean;
	/** Branch carries real committed content over its base branch
	 *  (a three-dot base...HEAD diff is non-empty). Commit count alone is not
	 *  enough — see module docstring. */
	branchDiffersFromBase: boolean;
	/** Current branch is the repo's default branch — nothing to ship from here. */
	isOnDefaultBranch: boolean;
	/** Branch is behind its upstream and must pull before it can ship. */
	needsPull: boolean;
	/** The Ship It action is wired up (handler provided by the host). */
	hasShipItHandler: boolean;
}

/**
 * Resolve whether the Ship It button should render.
 *
 * Shows for uncommitted changes, unmerged branch content, or an open PR on a non-default branch that is not behind its
 * upstream, and only when a handler is wired up.
 */
export function computeShowShipIt(input: ShipItVisibilityInput): boolean {
	const {
		isRepoConfirmed,
		hasChanges,
		branchDiffersFromBase,
		isOnDefaultBranch,
		needsPull,
		hasShipItHandler
	} = input;

	if (!hasShipItHandler) return false;
	// Never ship from a folder that isn't a confirmed repo — an empty or
	// cleaned-up worktree folder (or one whose status hasn't loaded) has nothing
	// to ship and the Ship It pipeline would have no repo to act on.
	if (!isRepoConfirmed) return false;
	// Nothing to ship from the default branch, and pulling must come first when behind.
	if (isOnDefaultBranch || needsPull) return false;

	// Something to ship = uncommitted work OR committed content the base lacks.
	return !!input.hasOpenPr || hasChanges || (!input.isHeadMerged && branchDiffersFromBase);
}
