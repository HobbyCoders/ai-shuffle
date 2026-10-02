/**
 * Unit tests for the pure git-action-option derivations.
 *
 * computeShipItSteps drives the read-only "what will happen" preview in the
 * Ship It option popover, so its branching (dirty tree → Commit step, missing PR
 * → Create PR step, cleanup preference → Clean up step) must stay honest.
 */
import { describe, it, expect } from 'vitest';
import { computeShipItSteps, computeOperationSideEffects, resolveCommitPlan, branchNameFromMessage } from './git-action-options';

const ids = (input: Parameters<typeof computeShipItSteps>[0]) =>
	computeShipItSteps(input).map((s) => s.id);

describe('computeShipItSteps', () => {
	it('omits push when origin already has all commits, naming the existing PR', () => {
		expect(computeShipItSteps({ hasChanges: false, unpushed: 0, hasOpenPR: true, prNumber: 42, cleanup: false }))
			.toEqual([{ id: 'merge', label: 'Merge PR #42' }]);
	});
	it('still pushes a new commit from a dirty but previously up-to-date branch', () => {
		expect(ids({ hasChanges: true, unpushed: 0, hasOpenPR: true, cleanup: false }))
			.toEqual(['commit', 'push', 'merge']);
	});
	it('creates a PR without pushing again when committed work is already remote', () => {
		expect(ids({ hasChanges: false, unpushed: 0, hasOpenPR: false, cleanup: true }))
			.toEqual(['pr', 'merge', 'cleanup']);
	});

	it('shows the full pipeline for a dirty branch with no PR and cleanup on', () => {
		expect(ids({ hasChanges: true, hasOpenPR: false, cleanup: true })).toEqual([
			'commit',
			'push',
			'pr',
			'merge',
			'cleanup'
		]);
	});

	it('drops the commit step when the tree is clean', () => {
		expect(ids({ hasChanges: false, hasOpenPR: false, cleanup: true })).toEqual([
			'push',
			'pr',
			'merge',
			'cleanup'
		]);
	});

	it('drops the create-PR step when a PR already exists', () => {
		expect(ids({ hasChanges: true, hasOpenPR: true, cleanup: true })).toEqual([
			'commit',
			'push',
			'merge',
			'cleanup'
		]);
	});

	it('drops the cleanup step when the cleanup preference is off', () => {
		expect(ids({ hasChanges: true, hasOpenPR: false, cleanup: false })).toEqual([
			'commit',
			'push',
			'pr',
			'merge'
		]);
	});

	it('keeps the push check when remote state is unknown', () => {
		expect(ids({ hasChanges: false, hasOpenPR: true, cleanup: false })).toEqual([
			'push',
			'merge'
		]);
	});

	it('gives every step a human label', () => {
		const steps = computeShipItSteps({ hasChanges: true, hasOpenPR: false, cleanup: true });
		for (const s of steps) {
			expect(s.label.length).toBeGreaterThan(0);
		}
	});
});

describe('computeOperationSideEffects', () => {
	const origin = { sessionId: 'sess-1', projectId: 'proj-1' };

	it('returns null for a non-done operation (failure is pinned elsewhere)', () => {
		expect(computeOperationSideEffects({ status: 'error' }, origin, origin)).toBeNull();
		expect(computeOperationSideEffects({ status: 'running' }, origin, origin)).toBeNull();
	});

	it('applies worktree-delete + refresh when still on the originating session', () => {
		const fx = computeOperationSideEffects(
			{ status: 'done', worktreeDeleted: true, prUrl: 'https://x/pull/7', prNumber: 7 },
			origin,
			{ sessionId: 'sess-1', projectId: 'proj-1' }
		);
		expect(fx).not.toBeNull();
		expect(fx!.markWorktreeDeleted).toBe(true);
		expect(fx!.refreshGit).toBe(true);
		expect(fx!.toast.label).toBe('Merged pull request #7');
		expect(fx!.toast.prUrl).toBe('https://x/pull/7');
	});

	it('suppresses session-scoped effects after the user switches session', () => {
		const fx = computeOperationSideEffects(
			{ status: 'done', worktreeDeleted: true, prUrl: 'https://x/pull/7', prNumber: 7 },
			origin,
			{ sessionId: 'sess-2', projectId: 'proj-1' } // switched away
		);
		// markWorktreeDeleted must NEVER apply to a non-originating session.
		expect(fx!.markWorktreeDeleted).toBe(false);
		expect(fx!.refreshGit).toBe(false);
		// The toast still fires (safe confirmation of the completed action).
		expect(fx!.toast.label).toBe('Merged pull request #7');
	});

	it('suppresses git refresh when the project changed but session matches', () => {
		const fx = computeOperationSideEffects(
			{ status: 'done', worktreeDeleted: false },
			origin,
			{ sessionId: 'sess-1', projectId: 'proj-OTHER' }
		);
		expect(fx!.refreshGit).toBe(false);
		expect(fx!.markWorktreeDeleted).toBe(false);
		expect(fx!.toast.label).toBe('Operation complete');
	});

	it('labels a merged op without a PR number generically', () => {
		const fx = computeOperationSideEffects(
			{ status: 'done', prUrl: 'https://x/pull/z' },
			origin,
			origin
		);
		expect(fx!.toast.label).toBe('Operation complete');
		expect(fx!.toast.prUrl).toBe('https://x/pull/z');
	});
});

describe('resolveCommitPlan (#1659)', () => {
	const base = { isOnDefaultBranch: true, needsPull: false };
	it('refuses a saved PR flow on the default branch instead of pushing to it', () => {
		const plan = resolveCommitPlan({ ...base, requested: 'pr' });
		expect(plan.blocked).toMatch(/default branch/);
	});
	it('refuses an unconfirmed push on the default branch', () => {
		expect(resolveCommitPlan({ ...base, requested: 'push' }).blocked).not.toBeNull();
	});
	it('allows an explicit push to the default branch', () => {
		expect(resolveCommitPlan({ ...base, requested: 'push', pushToDefaultBranch: true })).toEqual({ nextStep: 'push', newBranch: null, blocked: null });
	});
	it('an explicit push choice never upgrades to a PR from the default branch', () => {
		expect(resolveCommitPlan({ ...base, requested: 'pr', pushToDefaultBranch: true }).blocked).not.toBeNull();
	});
	it('commits on a new branch and keeps the PR step', () => {
		expect(resolveCommitPlan({ ...base, requested: 'pr', newBranch: ' feat/x ' })).toEqual({ nextStep: 'pr', newBranch: 'feat/x', blocked: null });
	});
	it('a new branch skips the pull-first downgrade (it has no upstream yet)', () => {
		expect(resolveCommitPlan({ ...base, needsPull: true, requested: 'push', newBranch: '' }).nextStep).toBe('push');
	});
	it('commit-only on the default branch is always fine', () => {
		expect(resolveCommitPlan({ ...base, requested: 'commit' }).blocked).toBeNull();
	});
	it('feature branches keep the saved flow; behind still means commit only', () => {
		expect(resolveCommitPlan({ isOnDefaultBranch: false, needsPull: false, requested: 'pr' }).nextStep).toBe('pr');
		expect(resolveCommitPlan({ isOnDefaultBranch: false, needsPull: true, requested: 'pr' }).nextStep).toBe('commit');
	});
});

describe('branchNameFromMessage', () => {
	it('uses the conventional-commit type as the prefix', () => {
		expect(branchNameFromMessage('fix(git): Guard the default branch!\n\nbody')).toBe('fix/guard-the-default-branch');
	});
	it('falls back to change/ for free-form subjects', () => {
		expect(branchNameFromMessage('Update README links')).toBe('change/update-readme-links');
	});
	it('caps the slug and never ends with a dash', () => {
		const name = branchNameFromMessage(`feat: ${'word '.repeat(30)}`);
		expect(name.length).toBeLessThanOrEqual(53);
		expect(name.endsWith('-')).toBe(false);
	});
	it('stamps an empty subject', () => {
		expect(branchNameFromMessage('!!!', new Date('2026-10-02T10:05:00Z'))).toBe('change/202610021005');
	});
});
