import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { workerGitTargetFromKey, workerGitTargetKey } from './worker-git-target';

describe('workerGitTargetKey', () => {
	it('gives equal keys for equal targets, so a replaced tab does not reload Git', () => {
		const first = { projectV2Id: 'pv2-a', deviceIntent: { id: 'dev-b' }, selectedWorktreeBranch: 'main' };
		const second = { ...first, deviceIntent: { ...first.deviceIntent }, draft: 'typing' } as typeof first;
		expect(workerGitTargetKey(second)).toBe(workerGitTargetKey(first));
		expect(workerGitTargetFromKey(workerGitTargetKey(first))).toEqual({ workerId: 'dev-b', branch: 'main' });
	});

	it('changes when the worker or branch changes', () => {
		const base = { projectV2Id: 'pv2-a' };
		expect(workerGitTargetKey(base)).not.toBe(workerGitTargetKey({ ...base, deviceIntent: { id: 'dev-b' } }));
		expect(workerGitTargetKey(base)).not.toBe(workerGitTargetKey({ ...base, worktreeBranch: 'feat/x' }));
		expect(workerGitTargetFromKey(workerGitTargetKey(base))).toEqual({ workerId: 'local', branch: null });
	});

	it('does not target a branch the first send is still creating (#803)', () => {
		// Auto worktree: the send names the new branch before its checkout exists.
		const draft = { projectV2Id: 'pv2-a' };
		const sending = { ...draft, worktreeBranch: 'feat/from-prompt', worktreeCreateNewBranch: true };
		expect(workerGitTargetKey(sending)).toBe(workerGitTargetKey(draft));
		expect(workerGitTargetFromKey(workerGitTargetKey(sending))).toEqual({ workerId: 'local', branch: null });
		// A legacy-project draft keeps its legacy load: no worker target at all.
		expect(workerGitTargetKey({ worktreeBranch: 'feat/new', worktreeCreateNewBranch: true })).toBeNull();
		// An existing branch picked for the worktree is still that checkout.
		expect(workerGitTargetFromKey(workerGitTargetKey({ ...draft, worktreeBranch: 'feat/x' })))
			.toEqual({ workerId: 'local', branch: 'feat/x' });
	});

	it('has no worker target for a started chat or a legacy worktree draft', () => {
		expect(workerGitTargetKey({ sessionId: 's1', projectV2Id: 'pv2-a' })).toBeNull();
		expect(workerGitTargetKey({ projectV2Id: 'pv2-a', worktreeId: 'wt-1' })).toBeNull();
		expect(workerGitTargetKey({})).toBeNull();
		expect(workerGitTargetKey(null)).toBeNull();
		expect(workerGitTargetFromKey(null)).toBeNull();
	});

	it('is what MainPanel derives its Git worker target from', () => {
		const source = readFileSync(resolve(process.cwd(), 'src/lib/components/layout/MainPanel.svelte'), 'utf8');
		expect(source).toMatch(/const selectedWorkerTargetKey = \$derived\(workerGitTargetKey\(activeTab\)\)/);
		expect(source).toMatch(/const selectedWorkerTarget = \$derived\(workerGitTargetFromKey\(selectedWorkerTargetKey\)\)/);
	});
});
