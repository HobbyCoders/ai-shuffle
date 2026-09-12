/**
 * Pure decision logic for the header's PRIMARY git action.
 *
 * The GitHeaderActions template used to encode the Commit → Finish → Create PR
 * priority as an inline `{#if}/{:else if}` chain. This module extracts that
 * decision so it can be unit-tested independently of the Svelte template and
 * kept in lockstep with pr-state (the session-scoped open-PR lookup).
 *
 * Exactly one primary action is returned:
 *
 * - `commit`    — uncommitted changes exist (highest priority: never lose work)
 * - `finish`    — tree is clean and an open PR's head is the current branch
 *                 ("Merge PR #N"); gated on GitHub's lazy mergeability verdict,
 *                 where null (= still computing) does NOT block.
 * - `create-pr` — tree is clean, no open PR, on a non-default branch that is
 *                 not behind upstream, with something to open a PR for:
 *                 commits ahead OR real content differing from base.
 * - `clean`     — nothing to do; the template renders the disabled placeholder.
 */
export type PrimaryGitAction = 'commit' | 'finish' | 'create-pr' | 'clean';

export interface PrimaryActionInput {
	/** Uncommitted working-tree changes (drives the Commit button). */
	hasChanges: boolean;
	/** Current HEAD is already included in a merged PR. */
	isHeadMerged?: boolean;
	/** An open PR's head matches the current branch (from /git/pr-state). */
	hasOpenPr: boolean;
	/** GitHub mergeability: true/false/null(unknown). Only false blocks Finish. */
	isMergeable: boolean | null;
	/** Commits ahead of the default branch (base...HEAD commit count). */
	commitsAhead: number;
	/** The branch carries a non-empty base...HEAD content diff. */
	differsFromBase: boolean;
	/** Current branch is the repo default — nothing to PR from here. */
	isOnDefaultBranch: boolean;
	/** Branch is behind its upstream — pull must come first. */
	needsPull: boolean;
}

export function computePrimaryAction(input: PrimaryActionInput): PrimaryGitAction {
	const {
		hasChanges,
		hasOpenPr,
		isMergeable,
		commitsAhead,
		differsFromBase,
		isOnDefaultBranch,
		needsPull
	} = input;

	// Uncommitted work always wins — the user may need to commit before any
	// PR flow makes sense, and losing sight of the Commit button risks confusion.
	if (hasChanges) return 'commit';

	if (hasOpenPr) return 'finish';
	if (input.isHeadMerged) return 'clean';

	if (!isOnDefaultBranch && !needsPull && (commitsAhead > 0 || differsFromBase)) {
		return 'create-pr';
	}

	return 'clean';
}

/**
 * Whether the Finish button should be disabled for mergeability.
 * GitHub computes `mergeable` lazily — null means "still computing", which must
 * NOT block the user (it resolves within seconds on real PRs).
 */
export function finishBlockedByMergeability(isMergeable: boolean | null): boolean {
	return isMergeable === false;
}
