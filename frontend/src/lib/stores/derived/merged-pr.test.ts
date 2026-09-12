import { describe, it, expect } from 'vitest';
import { transformGitStatus, transformPrState, prStateEqual } from '$lib/api/git';
import { isCurrentHeadMerged } from './merged-pr';
import { computePrimaryAction } from './git-primary-action';
import { computeShowShipIt } from './ship-it-visibility';

const sha = 'a'.repeat(40);
const merged = transformPrState({ current_branch: 'feat/x', merged_head_sha: sha });
const status = transformGitStatus({ is_git_repo: true, current_branch: 'feat/x', head_commit: sha.slice(0, 12) });

function actions(isHeadMerged: boolean, hasChanges = false, hasOpenPr = false) {
	return {
		primary: computePrimaryAction({ hasChanges, hasOpenPr, isHeadMerged, isMergeable: null,
			commitsAhead: 1, differsFromBase: true, isOnDefaultBranch: false, needsPull: false }),
		ship: computeShowShipIt({ hasChanges, isHeadMerged, isRepoConfirmed: true,
			branchDiffersFromBase: true, isOnDefaultBranch: false, needsPull: false, hasShipItHandler: true })
	};
}

describe('already-merged branch actions', () => {
	it('hides both actions despite squash-merge ancestry and a non-empty three-dot diff', () => {
		expect(actions(isCurrentHeadMerged(status, merged))).toEqual({ primary: 'clean', ship: false });
	});
	it('keeps Commit and Ship It for uncommitted changes after a merge', () => {
		expect(actions(isCurrentHeadMerged(status, merged), true)).toEqual({ primary: 'commit', ship: true });
	});
	it('restores both PR actions for a new commit on the same branch', () => {
		const next = { ...status, head_commit: 'b'.repeat(12) };
		expect(actions(isCurrentHeadMerged(next, merged))).toEqual({ primary: 'create-pr', ship: true });
	});
	it('does not apply a merged head from another checkout branch', () => {
		expect(isCurrentHeadMerged({ ...status, current_branch: 'feat/new' }, merged)).toBe(false);
	});
	it('accepts a full HEAD SHA as well as the status abbreviation', () => {
		expect(isCurrentHeadMerged({ ...status, head_commit: sha }, merged)).toBe(true);
	});
	it('does not infer a merge from unknown or too-short hashes', () => {
		for (const head_commit of [null, '', 'a']) {
			expect(isCurrentHeadMerged({ ...status, head_commit }, merged)).toBe(false);
		}
		expect(isCurrentHeadMerged(status, transformPrState({ current_branch: 'feat/x' }))).toBe(false);
	});
	it('keeps a new open PR actionable on a reused branch', () => {
		const open = { ...merged, open_pr: { number: 9, title: 'New work', url: '', mergeable: null, draft: false, state: 'OPEN' } };
		expect(isCurrentHeadMerged(status, open)).toBe(false);
		expect(actions(true, false, true).primary).toBe('finish');
	});
	it('preserves a known merged head across GitHub outages', () => {
		expect(isCurrentHeadMerged(status, { ...merged, lookup_failed: true, served_stale: true })).toBe(true);
	});
	it('notifies the store when only the merged head changes', () => {
		expect(prStateEqual(merged, { ...merged, merged_head_sha: 'b'.repeat(40) })).toBe(false);
		expect(prStateEqual(merged, { ...merged })).toBe(true);
	});
});
