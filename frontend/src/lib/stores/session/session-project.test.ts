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
	it('a chat this worker runs offers its own project', () => {
		expect(agentProjectOnThisWorker({ project: 'app', projectV2Id: 'pv2-theirs' }, projects, catalog)).toBe('app');
	});
	it('a chat another worker runs offers this worker\'s checkout of the same shared project', () => {
		// Its legacy id was `app` (an unrelated project here); the shared id links `mine`.
		expect(agentProjectOnThisWorker({ project: '', projectV2Id: 'pv2-theirs' }, projects, catalog)).toBe('mine');
	});
	it('no checkout here, or a catalog entry naming a project this worker lacks: no project agents', () => {
		expect(agentProjectOnThisWorker({ project: '', projectV2Id: 'pv2-elsewhere' }, projects, catalog)).toBeUndefined();
		expect(agentProjectOnThisWorker({ project: '', projectV2Id: 'pv2-unknown' }, projects, catalog)).toBeUndefined();
		expect(agentProjectOnThisWorker({ project: '', projectV2Id: 'pv2-theirs' }, [{ id: 'app' }], catalog)).toBeUndefined();
		expect(agentProjectOnThisWorker({ project: '', projectV2Id: null }, projects, catalog)).toBeUndefined();
	});
});
