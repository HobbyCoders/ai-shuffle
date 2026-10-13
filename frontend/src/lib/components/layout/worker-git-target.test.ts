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
