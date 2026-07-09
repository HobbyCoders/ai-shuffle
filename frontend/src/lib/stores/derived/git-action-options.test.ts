/**
 * Unit tests for the pure git-action-option derivations.
 *
 * computeShipItSteps drives the read-only "what will happen" preview in the
 * Ship It option popover, so its branching (dirty tree → Commit step, missing PR
 * → Create PR step, cleanup preference → Clean up step) must stay honest.
 */
import { describe, it, expect } from 'vitest';
import { computeShipItSteps, computeOperationSideEffects } from './git-action-options';

const ids = (input: Parameters<typeof computeShipItSteps>[0]) =>
	computeShipItSteps(input).map((s) => s.id);

describe('computeShipItSteps', () => {
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

	it('always keeps push and merge (minimal clean + existing-PR + no-cleanup case)', () => {
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
