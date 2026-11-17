import { describe, expect, it } from 'vitest';
import {
	agentProjectOnThisWorker,
	resolvePersistedProject,
	resolveSessionProject
} from './session-project';

describe('resolveSessionProject', () => {
	it('treats empty project + v2 id as remote-only, not unset', () => {
		expect(resolveSessionProject({
			projectId: '',
			projectV2Id: 'pv2-remote'
		})).toEqual({ project: '', projectV2Id: 'pv2-remote' });
		expect(resolveSessionProject({
			projectId: null,
			projectV2Id: 'pv2-remote'
		})).toEqual({ project: '', projectV2Id: 'pv2-remote' });
	});

	it('does not fall back to ephemeral local project when the session is v2-only', () => {
		expect(resolveSessionProject({
			projectId: null,
			projectV2Id: 'pv2-remote',
			ephemeralProject: 'proj-local',
			ephemeralProjectV2Id: null
		})).toEqual({ project: '', projectV2Id: 'pv2-remote' });
	});

	it('restores a remote-only compose from ephemeral when the caller omits ids', () => {
		expect(resolveSessionProject({
			ephemeralProject: '',
			ephemeralProjectV2Id: 'pv2-remote'
		})).toEqual({ project: '', projectV2Id: 'pv2-remote' });
	});

	it('keeps a local project id and optional v2 stamp', () => {
		expect(resolveSessionProject({
			projectId: 'proj-1',
			projectV2Id: 'pv2-1'
		})).toEqual({ project: 'proj-1', projectV2Id: 'pv2-1' });
	});

	it('does not inherit a leaving session when nothing is provided', () => {
		expect(resolveSessionProject({})).toEqual({ project: '', projectV2Id: null });
	});

	// #1659: who runs the chat decides, never a comparison of the two ids.
	it('holds the shared id alone for a chat another worker runs, whatever local id the row or cache names', () => {
		expect(resolveSessionProject({
			projectId: 'app', projectV2Id: 'pv2-theirs', isRemote: true
		})).toEqual({ project: '', projectV2Id: 'pv2-theirs' });
		expect(resolveSessionProject({
			ephemeralProject: 'app', ephemeralProjectV2Id: 'pv2-theirs', isRemote: true
		})).toEqual({ project: '', projectV2Id: 'pv2-theirs' });
	});

	it('keeps the local id of a chat this worker runs, and of one whose owner is not known yet', () => {
		for (const isRemote of [false, undefined]) {
			expect(resolveSessionProject({
				projectId: 'app', projectV2Id: 'pv2-former-identity', isRemote
			})).toEqual({ project: 'app', projectV2Id: 'pv2-former-identity' });
		}
	});

	it('keeps the local id of a chat another worker runs when there is no shared id to hold', () => {
		expect(resolveSessionProject({
			projectId: 'app', projectV2Id: null, isRemote: true
		})).toEqual({ project: 'app', projectV2Id: null });
	});
});

describe('resolvePersistedProject', () => {
	it('does not replace a v2-only snapshot with the last local default', () => {
		expect(resolvePersistedProject(
			{ project: '', projectV2Id: 'pv2-remote' },
			'proj-default'
		)).toEqual({ project: '', projectV2Id: 'pv2-remote' });
	});

	it('uses the last local default when the snapshot has no project at all', () => {
		expect(resolvePersistedProject(
			{ project: '', projectV2Id: null },
			'proj-default'
		)).toEqual({ project: 'proj-default', projectV2Id: null });
	});
});

describe('agentProjectOnThisWorker (#1659)', () => {
	const projects = [{ id: 'app' }, { id: 'mine' }];
	const catalog = [
		{ project_v2_id: 'pv2-theirs', local_project_id: 'mine' },
		{ project_v2_id: 'pv2-elsewhere', local_project_id: null }
	];
	const remote = (projectV2Id: string | null) => ({ project: '', projectV2Id });
	it('a chat this worker runs offers its own project', () => {
		expect(agentProjectOnThisWorker({ project: 'app', projectV2Id: 'pv2-theirs' }, 'app', projects, catalog)).toBe('app');
	});
	it('a chat another worker runs whose row id is this worker\'s checkout of its shared project offers that project', () => {
		expect(agentProjectOnThisWorker(remote('pv2-theirs'), 'mine', projects, catalog)).toBe('mine');
	});
	it('a chat another worker runs offers nothing when its row id names another project, or it has none', () => {
		// The collision: the row's legacy id is an unrelated project here.
		expect(agentProjectOnThisWorker(remote('pv2-theirs'), 'app', projects, catalog)).toBeUndefined();
		// Shared id only: not widened to this worker's checkout.
		expect(agentProjectOnThisWorker(remote('pv2-theirs'), null, projects, catalog)).toBeUndefined();
		expect(agentProjectOnThisWorker(remote('pv2-theirs'), undefined, projects, catalog)).toBeUndefined();
		// No checkout here, a catalog entry naming a project this worker lacks, no shared id.
		expect(agentProjectOnThisWorker(remote('pv2-elsewhere'), 'mine', projects, catalog)).toBeUndefined();
		expect(agentProjectOnThisWorker(remote('pv2-unknown'), 'mine', projects, catalog)).toBeUndefined();
		expect(agentProjectOnThisWorker(remote('pv2-theirs'), 'mine', [{ id: 'app' }], catalog)).toBeUndefined();
		expect(agentProjectOnThisWorker(remote(null), 'mine', projects, catalog)).toBeUndefined();
	});
});
