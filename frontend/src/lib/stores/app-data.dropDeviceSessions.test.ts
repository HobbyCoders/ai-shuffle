import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	get: vi.fn(),
	newChat: vi.fn(),
	activeState: { sessionId: 'session-a' as string | null },
}));

vi.mock('$lib/api/client', () => ({
	api: {
		get: mocks.get,
		post: vi.fn(),
		put: vi.fn(),
		delete: vi.fn(),
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
	{ id: 'session-a', project_id: 'project-a', instance_id: 'dead-install' },
	{ id: 'session-b', project_id: 'project-a', instance_id: 'dead-install' },
	{ id: 'session-c', project_id: 'project-b', instance_id: 'still-here' },
];

beforeEach(() => {
	vi.clearAllMocks();
	mocks.activeState.sessionId = 'session-a';
	mocks.get.mockImplementation(async (url: string) => {
		if (url.startsWith('/sessions/cursor')) {
			return {
				items: sessions,
				next_cursor: null,
				next_cursor_id: null,
				has_more: false,
			};
		}
		if (url === '/projects') return [];
		if (url === '/cluster/projects') return { projects: [] };
		return [];
	});
});

describe('appData.dropDeviceSessions', () => {
	it('removes chats from a deleted device and resets an open one', async () => {
		await appData.loadSessions();

		appData.dropDeviceSessions('dead-install');

		expect(appData.current.sessions.map((session) => session.id)).toEqual(['session-c']);
		expect(appData.current.sessionsTotal).toBe(1);
		expect(mocks.newChat).toHaveBeenCalledOnce();
	});
});
