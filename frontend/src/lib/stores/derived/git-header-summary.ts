/**
 * Pure decision logic for the header's single git button and the panel it
 * opens.
 *
 * The chat header shows ONE button for the whole git cluster. Its label is
 * whatever needs the user first, in this order:
 *
 *   1. the open PR has merge conflicts
 *   2. the branch is behind its upstream
 *   3. uncommitted changes (shown as +added −deleted)
 *   4. commits waiting to be pushed
 *   5. an open PR
 *   6. up to date
 *
 * Private-access approvals are not Git state: they show in the chat above the
 * composer and on the header's private-access icon (#1816).
 *
 * The same input also drives the Ship It pipeline strip inside the panel:
 * which steps are already done, which one runs next, and where a conflict
 * stops the pipeline.
 */

export type HeaderTone = 'blocked' | 'behind' | 'changes' | 'push' | 'pr' | 'idle';
export type HeaderIcon = 'alert' | 'pull' | 'branch' | 'push' | 'pr';

export interface HeaderSummaryInput {
	/** The open PR reports `mergeable: false`. */
	mergeBlocked: boolean;
	/** Behind the upstream branch (pull first). */
	needsPull: boolean;
	commitsBehind: number;
	/** Both ahead and behind upstream. */
	isDiverged: boolean;
	/** Uncommitted working-tree changes. */
	hasChanges: boolean;
	additions: number;
	deletions: number;
	filesChanged: number;
	/** Commits ahead of upstream with a clean tree and nothing to pull. */
	canPush: boolean;
	commitsAheadUpstream: number;
	/** The open PR for this branch, if any. */
	prNumber: number | null;
	/** Repository default branch, named in the "up to date" summary. */
	defaultBranch: string;
}

export interface HeaderSummary {
	tone: HeaderTone;
	icon: HeaderIcon;
	/** Short label on the header button. Empty for the changes tone, which
	 *  renders the +/− numbers itself. */
	label: string;
	/** Dimmer trailing text on the header button, if any. */
	detail: string | null;
	/** One sentence for the panel head, the button title and its accessible name. */
	summary: string;
}

export interface UncommittedChangesInput {
	diffStats: { additions: number; deletions: number; files_changed: number } | null | undefined;
	status:
		| { has_changes?: boolean; staged?: number; modified?: number; untracked?: number; conflicts?: number }
		| null
		| undefined;
	changedFiles: readonly unknown[] | null | undefined;
}

export interface UncommittedChanges {
	hasChanges: boolean;
	filesChanged: number;
}

/**
 * Whether the checkout has uncommitted work, and how many files it touches.
 *
 * Diff stats come from `git diff`, which never sees untracked files, so a tree
 * whose only change is a new file reads 0/0/0 there. The status carries the
 * untracked count, so it decides too. The changed-files list does not: commit
 * and push apply a fresh inline status but leave that list as it was, and
 * letting it decide would keep a just-committed tree reading as dirty.
 */
export function computeUncommittedChanges(input: UncommittedChangesInput): UncommittedChanges {
	const { diffStats, status, changedFiles } = input;
	const untracked = status?.untracked ?? 0;
	const statusFiles = (status?.staged ?? 0) + (status?.modified ?? 0) + untracked + (status?.conflicts ?? 0);
	const hasChanges =
		(diffStats?.additions ?? 0) > 0 ||
		(diffStats?.deletions ?? 0) > 0 ||
		(diffStats?.files_changed ?? 0) > 0 ||
		status?.has_changes === true ||
		statusFiles > 0;
	if (!hasChanges) return { hasChanges, filesChanged: 0 };
	if (changedFiles && changedFiles.length > 0) return { hasChanges, filesChanged: changedFiles.length };
	// Diff stats count tracked files only; add the untracked ones from status.
	const tracked = diffStats?.files_changed ?? 0;
	const fromDiff = tracked > 0 ? tracked + untracked : 0;
	// A dirty tree touches at least one file, whatever the counts say.
	return { hasChanges, filesChanged: Math.max(1, fromDiff || statusFiles) };
}

function plural(n: number, word: string): string {
	return `${n} ${word}${n === 1 ? '' : 's'}`;
}

export function computeHeaderSummary(input: HeaderSummaryInput): HeaderSummary {
	const {
		mergeBlocked,
		needsPull,
		commitsBehind,
		isDiverged,
		hasChanges,
		additions,
		deletions,
		filesChanged,
		canPush,
		commitsAheadUpstream,
		prNumber,
		defaultBranch
	} = input;

	if (mergeBlocked && prNumber !== null) {
		return {
			tone: 'blocked',
			icon: 'alert',
			label: `PR #${prNumber}`,
			detail: 'conflicts',
			summary: `PR #${prNumber} has merge conflicts`
		};
	}
	if (needsPull) {
		const behind = `${commitsBehind} behind`;
		return {
			tone: 'behind',
			icon: 'pull',
			label: isDiverged ? `${behind}, ${commitsAheadUpstream} ahead` : behind,
			detail: prNumber !== null ? `PR #${prNumber}` : null,
			summary: isDiverged
				? `Diverged from upstream: ${commitsAheadUpstream} ahead, ${commitsBehind} behind`
				: `${plural(commitsBehind, 'commit')} behind upstream${prNumber !== null ? ` · PR #${prNumber} open` : ''}`
		};
	}
	if (hasChanges) {
		const files = plural(filesChanged, 'file');
		return {
			tone: 'changes',
			icon: 'branch',
			// Only used when neither count is positive (e.g. an untracked binary).
			label: additions > 0 || deletions > 0 ? '' : files,
			detail: additions > 0 || deletions > 0 ? files : null,
			summary: `${files} changed`
		};
	}
	if (canPush) {
		return {
			tone: 'push',
			icon: 'push',
			label: `${commitsAheadUpstream} to push`,
			detail: null,
			summary: `${plural(commitsAheadUpstream, 'commit')} to push`
		};
	}
	if (prNumber !== null) {
		return {
			tone: 'pr',
			icon: 'pr',
			label: `PR #${prNumber}`,
			detail: 'open',
			summary: `PR #${prNumber} open`
		};
	}
	return {
		tone: 'idle',
		icon: 'branch',
		label: 'Up to date',
		detail: null,
		summary: `Up to date with ${defaultBranch}`
	};
}

/**
 * The header button for a worktree or branch session whose status reads
 * "not a repository". It names the branch instead of a git state, so the
 * button never renders as a bare icon. `pending` means the reading is a
 * placeholder from the previous session while this one loads.
 */
export function computeUnreadableCheckoutSummary(branch: string | null, pending: boolean): HeaderSummary {
	const name = branch || 'Worktree';
	return {
		tone: 'idle',
		icon: 'branch',
		label: name,
		detail: pending ? 'checking' : 'unavailable',
		// The panel head names the branch on the line above, so the sentence doesn't.
		summary: pending ? 'Checking this checkout…' : "This checkout can't be read right now"
	};
}

export type PipelineStepState = 'done' | 'next' | 'todo' | 'stop';
export interface PipelineStep {
	id: 'commit' | 'push' | 'pr' | 'merge';
	label: string;
	state: PipelineStepState;
}

export interface PipelineInput {
	hasChanges: boolean;
	canPush: boolean;
	hasOpenPr: boolean;
	/** Ship It is offered at all (something to ship, GitHub reachable). */
	showShipIt: boolean;
	mergeBlocked: boolean;
}

/**
 * The four Ship It stages with their current state. Steps before the next one
 * are done; with nothing to ship every step is a plain todo; a merge conflict
 * marks the merge step as the stop.
 */
export function computePipeline(input: PipelineInput): PipelineStep[] {
	const { hasChanges, canPush, hasOpenPr, showShipIt, mergeBlocked } = input;
	const next = !showShipIt ? -1 : hasChanges ? 0 : canPush ? 1 : hasOpenPr ? 3 : 2;
	const labels: Array<[PipelineStep['id'], string]> = [
		['commit', 'Commit'],
		['push', 'Push'],
		['pr', 'PR'],
		['merge', 'Merge']
	];
	return labels.map(([id, label], i) => {
		let state: PipelineStepState = 'todo';
		if (mergeBlocked && hasOpenPr && i === 3) state = 'stop';
		else if (next >= 0 && i < next) state = 'done';
		else if (i === next) state = 'next';
		return { id, label, state };
	});
}
