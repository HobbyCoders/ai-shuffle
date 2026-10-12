import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('./client', () => ({ api: { get: mocks.get, post: mocks.post } }));

import { anyRoleInstalled, installTeamTemplate, listTeamTemplates, normalizeTeamTemplate } from './teamTemplates';

const starter = (gp: string | null = null, fd: string | null = null) => ({
	id: 'starter-team',
	name: 'Starter team',
	description: 'Two general agents.',
	agents: [
		{ key: 'general-purpose', name: 'General Purpose', existing_agent_id: gp },
		{ key: 'frontend-designer', name: 'Frontend Designer', existing_agent_id: fd }
	]
});

beforeEach(() => vi.clearAllMocks());

describe('team templates client', () => {
	it('lists the contract shape', async () => {
		mocks.get.mockResolvedValue([starter()]);
		await expect(listTeamTemplates()).resolves.toEqual([starter()]);
		expect(mocks.get).toHaveBeenCalledWith('/agents/team-templates');
	});

	it('answers [] when the worker has no such endpoint', async () => {
		mocks.get.mockRejectedValue({ status: 404, detail: 'Not Found' });
		await expect(listTeamTemplates()).resolves.toEqual([]);
	});

	it('passes other failures on', async () => {
		mocks.get.mockRejectedValue({ status: 403, detail: 'Owner only' });
		await expect(listTeamTemplates()).rejects.toMatchObject({ status: 403 });
	});

	it('installs by id and returns the new state', async () => {
		mocks.post.mockResolvedValue(starter('a1', 'a2'));
		const result = await installTeamTemplate('starter-team');
		expect(mocks.post).toHaveBeenCalledWith('/agents/templates/starter-team/install');
		expect(anyRoleInstalled(result)).toBe(true);
	});

	it('knows when any role is already filled', () => {
		expect(anyRoleInstalled(starter())).toBe(false);
		expect(anyRoleInstalled(starter(null, 'fd-1'))).toBe(true);
	});

	it('drops unusable entries and fills missing fields', () => {
		expect(normalizeTeamTemplate(null)).toBeNull();
		expect(normalizeTeamTemplate({ id: 't', agents: [{ key: 'k' }, { name: 'no key' }] })).toEqual({
			id: 't',
			name: 't',
			description: '',
			agents: [{ key: 'k', name: 'k', existing_agent_id: null }]
		});
	});
});
