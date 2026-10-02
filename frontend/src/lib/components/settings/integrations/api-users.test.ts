/**
 * API users model, ported from ApiUserManager.test.ts: load failures,
 * create payloads, one-time key, field updates (clearing sends null),
 * rotate, delete, one draft at a time, and session ordering.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ApiUser } from '$lib/api/apiUsers';
import type { Session } from '$lib/api/sessions';

const apiUsers = vi.hoisted(() => ({
	getApiUsers: vi.fn(), createApiUser: vi.fn(), updateApiUser: vi.fn(), deleteApiUser: vi.fn(), rotateApiUserKey: vi.fn()
}));
vi.mock('$lib/api/apiUsers', () => apiUsers);
const apiGet = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', () => ({ api: { get: apiGet } }));
const copyText = vi.hoisted(() => vi.fn());
vi.mock('$lib/utils/clipboard', () => ({ copyText, COPY_ERROR_MESSAGE: 'Could not copy' }));

import { ApiUsersModel, DRAFT_ID, LOAD_ERROR, NAME_REQUIRED, sessionsFor, sessionLabel, relativeAgo } from './api-users.svelte';

const user = (over: Partial<ApiUser> = {}): ApiUser => ({
	id: 'u1', name: 'Discord Bot', username: null, description: 'Server bot', project_id: 'p1',
	profile_ids: ['a1'], is_active: true, model: null, effort: null,
	created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', last_used_at: null, ...over
});

let model: ApiUsersModel;
beforeEach(async () => {
	vi.clearAllMocks();
	apiUsers.getApiUsers.mockResolvedValue([user()]);
	apiUsers.createApiUser.mockResolvedValue({ ...user({ id: 'u2', name: 'Phone' }), api_key: 'sk-new-key' });
	apiUsers.updateApiUser.mockImplementation(async (id: string, body: Partial<ApiUser>) => ({ ...user({ id }), ...body }));
	apiUsers.deleteApiUser.mockResolvedValue(undefined);
	apiUsers.rotateApiUserKey.mockResolvedValue({ id: 'u1', api_key: 'sk-rotated' });
	apiGet.mockImplementation(async (path: string) => path === '/agents'
		? [{ id: 'a1', name: 'Helper' }, { id: 'a2', name: 'Planner' }]
		: [{ id: 'p1', name: 'Website', path: '/w' }]);
	copyText.mockResolvedValue(undefined);
	model = new ApiUsersModel();
	await model.load();
});

describe('load', () => {
	it('loads users, agents and projects', () => {
		expect(model.users.map((u) => u.name)).toEqual(['Discord Bot']);
		expect(model.projectName('p1')).toBe('Website');
		expect(model.profileName('a2')).toBe('Planner');
		expect(model.loading).toBe(false);
	});

	it('reports a load failure, and a later good load clears only that', async () => {
		apiUsers.getApiUsers.mockRejectedValueOnce(new Error('offline'));
		await model.load();
		expect(model.error).toBe(LOAD_ERROR);
		await model.load();
		expect(model.error).toBe('');
		model.error = 'Key store locked';
		await model.load();
		expect(model.error).toBe('Key store locked');
	});

	it('treats a refused load as forbidden', async () => {
		apiUsers.getApiUsers.mockRejectedValueOnce({ status: 403, detail: 'Admin only' });
		await model.load();
		expect(model.forbidden).toBe(true);
		expect(model.error).toBe('');
	});
});

describe('draft and create', () => {
	it('keeps one draft and requires a name', async () => {
		expect(model.addDraft()).toBe(DRAFT_ID);
		model.draft!.name = 'kept';
		model.addDraft();
		expect(model.draft!.name).toBe('kept');
		model.draft!.name = '   ';
		expect(await model.createDraft()).toBeNull();
		expect(model.draftError).toBe(NAME_REQUIRED);
		expect(apiUsers.createApiUser).not.toHaveBeenCalled();
	});

	it('creates with trimmed name, chosen project and agents, then reveals the key once', async () => {
		model.addDraft();
		model.draft!.name = '  Phone  ';
		model.draft!.projectId = 'p1';
		model.toggleDraftProfile('a2');
		expect(await model.createDraft()).toBe('u2');
		expect(apiUsers.createApiUser).toHaveBeenCalledWith({
			name: 'Phone', description: undefined, project_id: 'p1', profile_ids: ['a2'], model: null, effort: null
		});
		expect(model.draft).toBeNull();
		expect(model.revealed).toEqual({ userId: 'u2', key: 'sk-new-key' });
		await model.copyKey();
		expect(copyText).toHaveBeenCalledWith('sk-new-key');
		expect(model.copied).toBe(true);
		model.dismissKey();
		expect(model.revealed).toBeNull();
	});

	it('keeps the draft and the worker error when create fails', async () => {
		apiUsers.createApiUser.mockRejectedValue({ detail: 'Name already in use' });
		model.addDraft();
		model.draft!.name = 'Discord Bot';
		expect(await model.createDraft()).toBeNull();
		expect(model.draftError).toBe('Name already in use');
		expect(model.draft!.name).toBe('Discord Bot');
	});
});

describe('existing users', () => {
	it('sends a cleared description and project as null', async () => {
		expect(await model.update('u1', { description: '' })).toBe(true);
		expect(apiUsers.updateApiUser).toHaveBeenLastCalledWith('u1', { description: null });
		expect(await model.update('u1', { project_id: '' })).toBe(true);
		expect(apiUsers.updateApiUser).toHaveBeenLastCalledWith('u1', { project_id: null });
		await model.update('u1', { is_active: false });
		expect(apiUsers.updateApiUser).toHaveBeenLastCalledWith('u1', { is_active: false });
		expect(model.users[0].is_active).toBe(false);
	});

	it('refuses a blank name without a request', async () => {
		expect(await model.update('u1', { name: '  ' })).toBe(false);
		expect(model.error).toBe(NAME_REQUIRED);
		expect(apiUsers.updateApiUser).not.toHaveBeenCalled();
	});

	it('toggles agents and sends "Agent default" model as null', async () => {
		await model.toggleProfile(model.users[0], 'a1');
		expect(apiUsers.updateApiUser).toHaveBeenLastCalledWith('u1', { profile_ids: [] });
		await model.setModel(user({ effort: 'high' }), '');
		expect(apiUsers.updateApiUser).toHaveBeenLastCalledWith('u1', { model: null, effort: 'high' });
	});

	it('rotates and reveals the new key; a failure is an error', async () => {
		expect(await model.rotate('u1')).toBe(true);
		expect(model.revealed).toEqual({ userId: 'u1', key: 'sk-rotated' });
		apiUsers.rotateApiUserKey.mockRejectedValue({ detail: 'Key store locked' });
		expect(await model.rotate('u1')).toBe(false);
		expect(model.error).toBe('Key store locked');
	});

	it('deletes and drops the user', async () => {
		apiUsers.getApiUsers.mockResolvedValue([]);
		expect(await model.remove('u1')).toBe(true);
		expect(apiUsers.deleteApiUser).toHaveBeenCalledWith('u1');
		expect(model.users).toEqual([]);
	});
});

describe('sessions', () => {
	it("lists one user's sessions newest first, naive timestamps as UTC", () => {
		const sessions = [
			{ id: 'sess-old-1234', title: 'Older', api_user_id: 'u1', updated_at: '2026-01-01T00:00:00Z' },
			{ id: 'sess-new-5678', title: '', api_user_id: 'u1', updated_at: '2026-02-01T00:00:00' },
			{ id: 'other', title: 'Not mine', api_user_id: 'u9', updated_at: '2026-03-01T00:00:00Z' }
		] as unknown as Session[];
		const mine = sessionsFor(sessions, 'u1');
		expect(mine.map(sessionLabel)).toEqual(['sess-new', 'Older']);
		expect(relativeAgo('2026-02-01T00:00:00', Date.parse('2026-02-01T00:05:00Z'))).toBe('5m ago');
	});
});
