import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	get: vi.fn(),
	delete: vi.fn(),
	newChat: vi.fn(),
	dropMissingProject: vi.fn(),
	activeState: { sessionId: 'session-a' as string | null },
}));

vi.mock('$lib/api/client', () => ({
	api: {
		get: mocks.get,
		post: vi.fn(),
		put: vi.fn(),
		delete: mocks.delete,
	},
	searchSessionsPaginated: vi.fn(),
	renameSession: vi.fn(),
	setSessionActive: vi.fn(),
}));

vi.mock('./active-session', () => ({
	activeSession: {
		get current() { return mocks.activeState; },
		newChat: mocks.newChat,
		dropMissingProject: mocks.dropMissingProject,
		validateSelectedProfile: vi.fn(),
		linkDiscoveredProjects: vi.fn(),
	},
}));

import { appData } from './app-data.svelte';

const sessions = [
	{ id: 'session-a', project_id: 'project-a' },
	{ id: 'session-b', project_id: 'project-a' },
	{ id: 'session-c', project_id: 'project-b' },
];

const projects = [
	{ id: 'project-a', name: 'Project A' },
	{ id: 'project-b', name: 'Project B' },
];

beforeEach(() => {
	vi.clearAllMocks();
	mocks.activeState.sessionId = 'session-a';
	let deleted = false;
	mocks.delete.mockImplementation(async () => {
		deleted = true;
	});
	mocks.get.mockImplementation(async (url: string) => {
		if (url.startsWith('/sessions/cursor')) {
			return {
				items: sessions,
				next_cursor: null,
				next_cursor_id: null,
				has_more: false,
			};
		}
		if (url === '/projects') return deleted ? [projects[1]] : projects;
		if (url === '/cluster/projects') return { projects: [] };
		return [];
	});
});

describe('appData.deleteProject', () => {
	it('takes the chat list from the server, keeping another worker\'s chat of the same project', async () => {
		// peer-legacy carries the same legacy project id but runs on another
		// worker: the server keeps it, so the client must not drop or leave it.
		const before = [...sessions, { id: 'peer-legacy', project_id: 'project-a' }];
		const after = [before[2], before[3]];
		let deleted = false;
		mocks.delete.mockImplementation(async () => { deleted = true; });
		mocks.get.mockImplementation(async (url: string) => {
			if (url.startsWith('/sessions/cursor')) {
				const items = deleted ? after : before;
				return { items, total: items.length, next_cursor: null, next_cursor_id: null, has_more: false };
			}
			if (url === '/projects') return deleted ? [projects[1]] : projects;
			if (url === '/cluster/projects') return { projects: [] };
			return [];
		});
		mocks.activeState.sessionId = 'peer-legacy';
		await appData.loadSessions();
		await appData.loadProjects();

		await appData.deleteProject('project-a');

		expect(mocks.delete).toHaveBeenCalledWith('/projects/project-a');
		expect(appData.current.projects.map((project) => project.id)).toEqual(['project-b']);
		expect(appData.current.sessions.map((session) => session.id).sort()).toEqual(['peer-legacy', 'session-c']);
		// Leaving a deleted open chat is the per-chat deletion broadcast's job.
		expect(mocks.newChat).not.toHaveBeenCalled();
		// The new compose is reconciled once the reloaded catalog is in.
		expect(mocks.dropMissingProject).toHaveBeenCalled();
	});

	it('drops this worker from the deleted project catalog row before any reload answers (#434)', async () => {
		const self = { install_id: 'me', device_name: 'Me', online: true, is_self: true };
		const peer = { install_id: 'peer', device_name: 'Peer', online: true, is_self: false };
		const catalog = [
			{ project_v2_id: 'pv2-a', origin: 'git:a', name: 'Project A', local_project_id: 'project-a', remote_only: false, eligible: [self] },
			{ project_v2_id: 'pv2-shared', origin: 'git:s', name: 'Shared', local_project_id: 'project-a', remote_only: false, eligible: [self, peer] },
			{ project_v2_id: 'pv2-b', origin: 'git:b', name: 'Project B', local_project_id: 'project-b', remote_only: false, eligible: [self] },
		];
		let release!: () => void;
		const held = new Promise<void>((resolve) => { release = resolve; });
		let deleted = false;
		mocks.delete.mockImplementation(async () => { deleted = true; });
		mocks.get.mockImplementation(async (url: string) => {
			if (url.startsWith('/sessions/cursor')) return { items: sessions, next_cursor: null, next_cursor_id: null, has_more: false };
			if (url === '/projects') return deleted ? [projects[1]] : projects;
			if (url === '/cluster/projects') {
				if (deleted) await held;
				return { projects: catalog };
			}
			return [];
		});
		await appData.loadProjects();
		await appData.loadClusterProjects();

		const done = appData.deleteProject('project-a');
		await vi.waitFor(() => expect(appData.current.projects.map((p) => p.id)).toEqual(['project-b']));

		// While the catalog reload is still in flight, no selector may offer the
		// deleted project as "available on this device".
		expect(appData.current.clusterProjects).toEqual([
			{ ...catalog[1], local_project_id: null, remote_only: true, eligible: [peer] },
			catalog[2],
		]);
		release();
		await done;
	});
});
