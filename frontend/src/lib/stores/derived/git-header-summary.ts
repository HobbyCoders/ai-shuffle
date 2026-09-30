/**
 * Pure decision logic for the header's single git button and the panel it
 * opens.
 *
 * The chat header shows ONE button for the whole git cluster. Its label is
 * whatever needs the user first, in this order:
 *
 *   1. private-access approvals waiting
 *   2. the open PR has merge conflicts
 *   3. the branch is behind its upstream
 *   4. uncommitted changes (shown as +added −deleted)
 *   5. commits waiting to be pushed
 *   6. an open PR
 *   7. up to date
 *
 * The same input also drives the Ship It pipeline strip inside the panel:
 * which steps are already done, which one runs next, and where a conflict
 * stops the pipeline.
 */

export type HeaderTone = 'attn' | 'blocked' | 'behind' | 'changes' | 'push' | 'pr' | 'idle';
export type HeaderIcon = 'shield' | 'alert' | 'pull' | 'branch' | 'push' | 'pr';

export interface HeaderSummaryInput {
	/** Private-access requests and one-shot actions waiting for an answer. */
	accessPending: number;
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

function plural(n: number, word: string): string {
	return `${n} ${word}${n === 1 ? '' : 's'}`;
}

export function computeHeaderSummary(input: HeaderSummaryInput): HeaderSummary {
	const {
		accessPending,
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

	if (accessPending > 0) {
		return {
			tone: 'attn',
			icon: 'shield',
			label: `${accessPending} to approve`,
			detail: null,
			summary: `${plural(accessPending, 'access request')} waiting for your answer`
		};
	}
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
