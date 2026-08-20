import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	get: vi.fn(),
	delete: vi.fn(),
	newChat: vi.fn(),
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
		validateSelectedProfile: vi.fn(),
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
		return [];
	});
});

describe('appData.deleteProject', () => {
	it('removes the project chats from sidebar state and resets an open deleted chat', async () => {
		await appData.loadSessions();
		await appData.loadProjects();

		await appData.deleteProject('project-a');

		expect(mocks.delete).toHaveBeenCalledWith('/projects/project-a');
		expect(appData.current.projects.map((project) => project.id)).toEqual(['project-b']);
		expect(appData.current.sessions.map((session) => session.id)).toEqual(['session-c']);
		expect(appData.current.sessionsTotal).toBe(1);
		expect(mocks.newChat).toHaveBeenCalledOnce();
	});
});
