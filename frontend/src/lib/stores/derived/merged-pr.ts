import type { GitStatus, PrState } from '$lib/api/git';

/** A squash merge leaves the original branch ahead of main by ancestry. */
export function isCurrentHeadMerged(status: GitStatus | null, prState: PrState | null): boolean {
	const head = status?.head_commit;
	const mergedHead = prState?.merged_head_sha;
	return !!(
		status?.current_branch &&
		status.current_branch === prState?.current_branch &&
		!prState.open_pr &&
		head && head.length >= 12 &&
		mergedHead && mergedHead.startsWith(head)
	);
}
