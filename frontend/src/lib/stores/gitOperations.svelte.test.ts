/**
 * Tests for the gitOperations store — the single active-operation tracker that
 * drives GitProgressPopover.
 *
 * Covers:
 *  1. Server-polled operations transition through poll snapshots (running →
 *     running → done) and mirror the server's step list + result.
 *  2. A failing operation pins its error and stays open (no auto-clear).
 *  3. `git.setOperationInProgress` is coupled to the operation lifecycle.
 *
 * The repo's convention is to mock the API module (see git.svelte.test.ts). Here
 * we override `getGitOperation` so `runServer` polls a canned sequence; the git
 * store (imported for the coupling assertions) keeps its real implementation.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { GitOperation } from '$lib/api/git';

const mocks = vi.hoisted(() => ({ getGitOperation: vi.fn() }));

vi.mock('$lib/api/git', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/git')>();
	return { ...actual, getGitOperation: mocks.getGitOperation };
});

import { gitOperations } from './gitOperations.svelte';
import { git } from './git.svelte';

function op(partial: Partial<GitOperation>): GitOperation {
	return {
		operation_id: 'op-1',
		status: 'running',
		steps: [],
		...partial
	};
}

beforeEach(() => {
	vi.useFakeTimers();
	mocks.getGitOperation.mockReset();
	gitOperations.clear();
});

afterEach(() => {
	gitOperations.clear();
	vi.useRealTimers();
});

describe('gitOperations — client-side coupling', () => {
	it('couples git.setOperationInProgress to begin/succeed/fail', () => {
		expect(git.operationInProgress).toBe(false);

		gitOperations.begin('Commit', [{ id: 'commit', label: 'Commit changes' }]);
		expect(git.operationInProgress).toBe(true);
		expect(gitOperations.current.status).toBe('running');

		gitOperations.setStep('commit', 'done');
		gitOperations.succeed();
		// Success with visible steps stays for the collapse window, poller resumes.
		expect(git.operationInProgress).toBe(false);
		expect(gitOperations.current.status).toBe('done');
		expect(gitOperations.current.active).toBe(true);

		// Collapses back to idle after the success delay.
		vi.advanceTimersByTime(2_500);
		expect(gitOperations.current.status).toBe('idle');
		expect(gitOperations.current.active).toBe(false);
	});

	it('clears immediately on success when there are no visible steps', () => {
		gitOperations.begin('Push', []);
		expect(git.operationInProgress).toBe(true);
		gitOperations.succeed();
		expect(gitOperations.current.status).toBe('idle');
		expect(git.operationInProgress).toBe(false);
	});

	it('pins the running step on failCurrent and stays open', () => {
		gitOperations.begin('Commit', [
			{ id: 'commit', label: 'Commit changes' },
			{ id: 'push', label: 'Push to remote' }
		]);
		gitOperations.setStep('commit', 'done');
		gitOperations.setStep('push', 'running');
		gitOperations.failCurrent('remote rejected', 'non-fast-forward');

		const s = gitOperations.current;
		expect(s.status).toBe('error');
		expect(s.active).toBe(true);
		const pushStep = s.steps.find((st) => st.id === 'push');
		expect(pushStep?.status).toBe('error');
		expect(pushStep?.error).toBe('remote rejected');
		expect(pushStep?.detail).toBe('non-fast-forward');
		expect(git.operationInProgress).toBe(false);

		// Error state does not auto-clear.
		vi.advanceTimersByTime(10_000);
		expect(gitOperations.current.status).toBe('error');
	});
});

describe('gitOperations — server-polled operations', () => {
	it('mirrors poll snapshots through running → done and captures the result', async () => {
		mocks.getGitOperation
			.mockResolvedValueOnce(
				op({ status: 'running', steps: [{ id: 'merge', label: 'Merge PR', status: 'running' }] })
			)
			.mockResolvedValueOnce(
				op({
					status: 'done',
					steps: [{ id: 'merge', label: 'Merge PR', status: 'done' }],
					result: { pr_url: 'https://github.com/x/y/pull/7', pr_number: 7, worktree_deleted: true }
				})
			);

		const starter = vi.fn().mockResolvedValue(
			op({ status: 'running', steps: [{ id: 'merge', label: 'Merge PR', status: 'pending' }] })
		);

		const p = gitOperations.runServer('proj-1', 'Finish', starter);

		// Let the kickoff resolve so its steps are mirrored.
		await vi.advanceTimersByTimeAsync(0);
		expect(gitOperations.current.status).toBe('running');
		expect(git.operationInProgress).toBe(true);

		// First poll → still running.
		await vi.advanceTimersByTimeAsync(1_000);
		expect(gitOperations.current.steps[0].status).toBe('running');
		expect(gitOperations.current.status).toBe('running');

		// Second poll → done.
		await vi.advanceTimersByTimeAsync(1_000);
		await p;

		const s = gitOperations.current;
		expect(s.status).toBe('done');
		expect(s.steps[0].status).toBe('done');
		expect(s.result?.pr_number).toBe(7);
		expect(s.result?.worktree_deleted).toBe(true);
		expect(git.operationInProgress).toBe(false);
	});

	it('fails and pins the failing step when the server operation errors', async () => {
		mocks.getGitOperation.mockResolvedValueOnce(
			op({
				status: 'error',
				steps: [
					{ id: 'push', label: 'Push', status: 'done' },
					{ id: 'merge', label: 'Merge PR', status: 'error', error: 'merge conflict', detail: 'file.ts' }
				]
			})
		);

		const starter = vi.fn().mockResolvedValue(
			op({ status: 'running', steps: [{ id: 'merge', label: 'Merge PR', status: 'pending' }] })
		);

		const p = gitOperations.runServer('proj-1', 'Ship It', starter);
		await vi.advanceTimersByTimeAsync(0);
		await vi.advanceTimersByTimeAsync(1_000);
		await p;

		const s = gitOperations.current;
		expect(s.status).toBe('error');
		expect(s.error).toBe('merge conflict');
		const mergeStep = s.steps.find((st) => st.id === 'merge');
		expect(mergeStep?.status).toBe('error');
		expect(mergeStep?.detail).toBe('file.ts');
		expect(git.operationInProgress).toBe(false);
	});
});
