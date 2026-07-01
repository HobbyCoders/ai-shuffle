/**
 * Regression: the backend can rewrite a new project's `id` on create — when the
 * requested id already exists for a different install in shared Postgres it
 * auto-suffixes `{id}-{sha256(instance)[:6]}`. GET /projects is instance
 * filtered, so a caller that keys off the requested id selects a project the
 * list never returns → picker falls back to "no project".
 *
 * createProject must therefore return the Project parsed from the POST response
 * so callers (AddProjectModal → onCreated) can select the server-assigned id.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Project } from './types';

const mocks = vi.hoisted(() => ({
	post: vi.fn(),
	get: vi.fn(() => Promise.resolve([])),
}));

vi.mock('$lib/api/client', () => ({
	api: {
		post: mocks.post,
		get: mocks.get,
		put: vi.fn(),
		delete: vi.fn(),
	},
	// app-data re-exports these from the client module; provide inert stubs.
	searchSessionsPaginated: vi.fn(),
}));

// app-data imports activeSession at module load (used only in deleteProfile).
vi.mock('./active-session', () => ({
	activeSession: { validateSelectedProfile: vi.fn() },
}));

import { appData } from './app-data.svelte';

function makeProject(id: string): Project {
	return {
		id,
		name: 'My Project',
		path: '/tmp/my-project',
		settings: {},
		created_at: '2026-01-01T00:00:00Z',
		updated_at: '2026-01-01T00:00:00Z',
	};
}

beforeEach(() => {
	mocks.post.mockReset();
	mocks.get.mockReset();
	mocks.get.mockResolvedValue([]);
	vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('appData.createProject', () => {
	it('returns the server-assigned Project when the id is rewritten on create', async () => {
		// Backend collided on "my-project" and auto-suffixed the id.
		const serverProject = makeProject('my-project-a1b2c3');
		mocks.post.mockResolvedValue(serverProject);

		const created = await appData.createProject({
			id: 'my-project',
			name: 'My Project',
			path: '/tmp/my-project',
		});

		expect(mocks.post).toHaveBeenCalledWith('/projects', {
			id: 'my-project',
			name: 'My Project',
			path: '/tmp/my-project',
		});
		// The returned id is the server's, not the requested one — this is the id
		// AddProjectModal forwards to onCreated so the picker can select it.
		expect(created.id).toBe('my-project-a1b2c3');
		// Projects are reloaded so the new row is present for selection.
		expect(mocks.get).toHaveBeenCalledWith('/projects');
	});

	it('returns the Project unchanged when the id is not rewritten', async () => {
		const serverProject = makeProject('my-project');
		mocks.post.mockResolvedValue(serverProject);

		const created = await appData.createProject({
			id: 'my-project',
			name: 'My Project',
			path: '/tmp/my-project',
		});

		expect(created.id).toBe('my-project');
	});
});
