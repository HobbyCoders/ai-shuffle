import { describe, expect, it } from 'vitest';
import {
	resolveNewChatProject,
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

describe('resolveNewChatProject', () => {
	it('restores a v2-only new-chat compose from ephemeral', () => {
		expect(resolveNewChatProject(
			{ project: '', projectV2Id: 'pv2-remote' },
			'proj-default'
		)).toEqual({ project: '', projectV2Id: 'pv2-remote' });
	});

	it('uses the last local default when ephemeral is a local pick', () => {
		expect(resolveNewChatProject(
			{ project: 'proj-1', projectV2Id: 'pv2-1' },
			'proj-default'
		)).toEqual({ project: 'proj-default', projectV2Id: null });
	});
});
