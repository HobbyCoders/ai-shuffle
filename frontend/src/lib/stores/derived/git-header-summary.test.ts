import { describe, it, expect } from 'vitest';
import { computeCompactBadge, computeHeaderSummary, computePipeline, computeUncommittedChanges, computeUnreadableCheckoutSummary, type HeaderSummaryInput } from './git-header-summary';

const base: HeaderSummaryInput = {
	mergeBlocked: false,
	needsPull: false,
	commitsBehind: 0,
	isDiverged: false,
	hasChanges: false,
	additions: 0,
	deletions: 0,
	filesChanged: 0,
	canPush: false,
	commitsAheadUpstream: 0,
	prNumber: null,
	defaultBranch: 'main'
};

describe('computeHeaderSummary priority ladder', () => {
	it('reads "up to date" when nothing needs the user', () => {
		const s = computeHeaderSummary(base);
		expect(s.tone).toBe('idle');
		expect(s.label).toBe('Up to date');
		expect(s.summary).toBe('Up to date with main');
	});

	it('names an open PR when that is the only state', () => {
		const s = computeHeaderSummary({ ...base, prNumber: 12 });
		expect(s.tone).toBe('pr');
		expect(s.label).toBe('PR #12');
		expect(s.detail).toBe('open');
	});

	it('prefers commits to push over an open PR', () => {
		const s = computeHeaderSummary({ ...base, prNumber: 12, canPush: true, commitsAheadUpstream: 2 });
		expect(s.tone).toBe('push');
		expect(s.label).toBe('2 to push');
		expect(s.summary).toBe('2 commits to push');
	});

	it('prefers uncommitted changes over commits to push, rendering the numbers itself', () => {
		const s = computeHeaderSummary({ ...base, canPush: true, commitsAheadUpstream: 2, hasChanges: true, additions: 184, deletions: 37, filesChanged: 4 });
		expect(s.tone).toBe('changes');
		expect(s.label).toBe('');
		expect(s.detail).toBe('4 files');
		expect(s.summary).toBe('4 files changed');
	});

	it('falls back to the file count when a change has no line counts (binary)', () => {
		const s = computeHeaderSummary({ ...base, hasChanges: true, filesChanged: 1 });
		expect(s.label).toBe('1 file');
		expect(s.detail).toBeNull();
	});

	it('prefers "behind" over uncommitted changes and names the PR alongside', () => {
		const s = computeHeaderSummary({ ...base, hasChanges: true, additions: 3, filesChanged: 1, needsPull: true, commitsBehind: 1, prNumber: 7 });
		expect(s.tone).toBe('behind');
		expect(s.label).toBe('1 behind');
		expect(s.detail).toBe('PR #7');
		expect(s.summary).toBe('1 commit behind upstream · PR #7 open');
	});

	it('describes a diverged branch with both counts', () => {
		const s = computeHeaderSummary({ ...base, needsPull: true, commitsBehind: 2, isDiverged: true, commitsAheadUpstream: 3 });
		expect(s.label).toBe('2 behind, 3 ahead');
		expect(s.summary).toContain('Diverged');
	});

	it('prefers merge conflicts over being behind', () => {
		const s = computeHeaderSummary({ ...base, needsPull: true, commitsBehind: 1, mergeBlocked: true, prNumber: 9 });
		expect(s.tone).toBe('blocked');
		expect(s.label).toBe('PR #9');
		expect(s.detail).toBe('conflicts');
	});

	it('ignores a merge block with no PR to name', () => {
		const s = computeHeaderSummary({ ...base, mergeBlocked: true });
		expect(s.tone).toBe('idle');
	});

	it('leads with a merge conflict: private access is not Git state (#1816)', () => {
		const s = computeHeaderSummary({ ...base, mergeBlocked: true, prNumber: 9, hasChanges: true, additions: 1, filesChanged: 1 });
		expect(s.tone).toBe('blocked');
		expect(s.label).toBe('PR #9');
		expect('accessPending' in base).toBe(false);
	});
});

describe('computeUncommittedChanges', () => {
	const zero = { additions: 0, deletions: 0, files_changed: 0 };
	const cleanStatus = { has_changes: false, staged: 0, modified: 0, untracked: 0, conflicts: 0 };

	it('reads a clean tree as clean', () => {
		expect(computeUncommittedChanges({ diffStats: zero, status: cleanStatus, changedFiles: [] }))
			.toEqual({ hasChanges: false, filesChanged: 0 });
	});

	it('counts an untracked-only tree that diff stats cannot see', () => {
		const r = computeUncommittedChanges({
			diffStats: zero,
			status: { ...cleanStatus, has_changes: true, untracked: 1 },
			changedFiles: [{ file: 'new.png', status: '?', category: 'untracked' }]
		});
		expect(r).toEqual({ hasChanges: true, filesChanged: 1 });
	});

	it('counts untracked files from status before the changed-files list loads', () => {
		const r = computeUncommittedChanges({
			diffStats: zero,
			status: { ...cleanStatus, has_changes: true, untracked: 2 },
			changedFiles: null
		});
		expect(r).toEqual({ hasChanges: true, filesChanged: 2 });
	});

	it('adds untracked files to the tracked count from diff stats', () => {
		const r = computeUncommittedChanges({
			diffStats: { additions: 3, deletions: 1, files_changed: 2 },
			status: { ...cleanStatus, has_changes: true, modified: 2, untracked: 1 },
			changedFiles: []
		});
		expect(r).toEqual({ hasChanges: true, filesChanged: 3 });
	});

	it('prefers the changed-files list for the count', () => {
		const r = computeUncommittedChanges({
			diffStats: { additions: 3, deletions: 1, files_changed: 2 },
			status: { ...cleanStatus, has_changes: true, modified: 2 },
			changedFiles: [{}, {}]
		});
		expect(r.filesChanged).toBe(2);
	});

	it('does not let a stale changed-files list keep a committed tree dirty', () => {
		// Commit applies a fresh inline status and diff stats but leaves the list.
		const r = computeUncommittedChanges({
			diffStats: zero,
			status: cleanStatus,
			changedFiles: [{ file: 'new.png', status: '?', category: 'untracked' }]
		});
		expect(r).toEqual({ hasChanges: false, filesChanged: 0 });
	});

	it('still works from diff stats alone when status has not loaded', () => {
		const r = computeUncommittedChanges({
			diffStats: { additions: 5, deletions: 2, files_changed: 1 },
			status: null,
			changedFiles: null
		});
		expect(r).toEqual({ hasChanges: true, filesChanged: 1 });
	});

	it('feeds a file-count label to the header when line counts are zero', () => {
		const r = computeUncommittedChanges({
			diffStats: zero,
			status: { ...cleanStatus, has_changes: true, untracked: 1 },
			changedFiles: null
		});
		const s = computeHeaderSummary({ ...base, hasChanges: r.hasChanges, filesChanged: r.filesChanged });
		expect(s.tone).toBe('changes');
		expect(s.label).toBe('1 file');
		expect(s.summary).toBe('1 file changed');
	});
});

describe('computePipeline', () => {
	const states = (steps: ReturnType<typeof computePipeline>) => steps.map((s) => s.state);

	it('lights Commit next for a dirty tree', () => {
		expect(states(computePipeline({ hasChanges: true, canPush: false, hasOpenPr: false, showShipIt: true, mergeBlocked: false })))
			.toEqual(['next', 'todo', 'todo', 'todo']);
	});

	it('marks Commit done and Push next with commits waiting', () => {
		expect(states(computePipeline({ hasChanges: false, canPush: true, hasOpenPr: false, showShipIt: true, mergeBlocked: false })))
			.toEqual(['done', 'next', 'todo', 'todo']);
	});

	it('lights PR next for a pushed branch with no PR', () => {
		expect(states(computePipeline({ hasChanges: false, canPush: false, hasOpenPr: false, showShipIt: true, mergeBlocked: false })))
			.toEqual(['done', 'done', 'next', 'todo']);
	});

	it('lights Merge next for an open PR and stops there on conflicts', () => {
		expect(states(computePipeline({ hasChanges: false, canPush: false, hasOpenPr: true, showShipIt: true, mergeBlocked: false })))
			.toEqual(['done', 'done', 'done', 'next']);
		expect(states(computePipeline({ hasChanges: false, canPush: false, hasOpenPr: true, showShipIt: true, mergeBlocked: true })))
			.toEqual(['done', 'done', 'done', 'stop']);
	});

	it('leaves every step plain when there is nothing to ship', () => {
		expect(states(computePipeline({ hasChanges: false, canPush: false, hasOpenPr: false, showShipIt: false, mergeBlocked: false })))
			.toEqual(['todo', 'todo', 'todo', 'todo']);
	});
});

describe('computeUnreadableCheckoutSummary', () => {
	it('names the branch instead of a git state, never an empty label', () => {
		const s = computeUnreadableCheckoutSummary('feature/x', false);
		expect(s.label).toBe('feature/x');
		expect(s.detail).toBe('unavailable');
		expect(s.icon).toBe('branch');
		expect(s.summary).toBe("This checkout can't be read right now");
	});

	it('says it is checking while a placeholder revalidates', () => {
		const s = computeUnreadableCheckoutSummary('feature/x', true);
		expect(s.detail).toBe('checking');
		expect(s.summary).toBe('Checking this checkout…');
	});

	it('falls back to a generic label when no branch is known', () => {
		expect(computeUnreadableCheckoutSummary(null, false).label).toBe('Worktree');
		expect(computeUnreadableCheckoutSummary('', true).label).toBe('Worktree');
	});
});

describe('computeCompactBadge (phone bar Git button)', () => {
	const counts = { filesChanged: 3, commitsAhead: 2, commitsBehind: 4 };
	const tone = (t: Parameters<typeof computeCompactBadge>[0]['tone']) => ({ tone: t, icon: 'branch' as const, label: '', detail: null, summary: '' });

	it('counts what the full button would name', () => {
		expect(computeCompactBadge(tone('changes'), counts)).toBe('3');
		expect(computeCompactBadge(tone('push'), counts)).toBe('↑2');
		expect(computeCompactBadge(tone('behind'), counts)).toBe('↓4');
		expect(computeCompactBadge(tone('blocked'), counts)).toBe('!');
		expect(computeCompactBadge(tone('idle'), counts)).toBe('✓');
	});

	it('lets an open PR glyph speak for itself', () => {
		expect(computeCompactBadge(tone('pr'), counts)).toBeNull();
	});

	it('caps long counts and never shows zero files for a dirty tree', () => {
		expect(computeCompactBadge(tone('changes'), { ...counts, filesChanged: 140 })).toBe('99+');
		expect(computeCompactBadge(tone('changes'), { ...counts, filesChanged: 0 })).toBe('1');
	});

	it('shows nothing for a checkout that cannot be read', () => {
		expect(computeCompactBadge(computeUnreadableCheckoutSummary('feat/x', false), counts, false)).toBeNull();
	});
});
