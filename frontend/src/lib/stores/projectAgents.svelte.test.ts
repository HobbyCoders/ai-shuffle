/**
 * Project agents store — list loading, create/delete, error surfacing, and the
 * stale-response guard when the Agents tab switches project mid-request.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { ProjectAgent } from '$lib/api/projectAgents';

const api = vi.hoisted(() => ({
	getProjectAgents: vi.fn(),
	createProjectAgent: vi.fn(),
	deleteProjectAgent: vi.fn()
}));

vi.mock('$lib/api/projectAgents', () => api);

import { projectAgents, allProjectAgents, projectAgentCount, projectAgentsError } from './projectAgents.svelte';

function agent(name: string): ProjectAgent {
	return { name, description: `${name} agent`, model: null, tools: null, file_path: `.fluid/agents/${name}.md`, created_at: null, updated_at: null };
}

function deferred<T>() {
	let resolve!: (v: T) => void;
	let reject!: (e: unknown) => void;
	const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
	return { promise, resolve, reject };
}

beforeEach(() => {
	for (const fn of Object.values(api)) fn.mockReset();
	projectAgents.reset();
});

describe('projectAgents store', () => {
	it('starts empty', () => {
		expect(projectAgents.current).toMatchObject({ agents: [], selectedAgent: null, currentProjectId: null, loading: false, saving: false, error: null });
		expect(projectAgentCount.current).toBe(0);
	});

	it('loads the agents of a project', async () => {
		api.getProjectAgents.mockResolvedValue([agent('a'), agent('b')]);
		await projectAgents.loadAgents('P1');
		expect(api.getProjectAgents).toHaveBeenCalledWith('P1');
		expect(projectAgents.current).toMatchObject({ currentProjectId: 'P1', loading: false, error: null });
		expect(allProjectAgents.current.map((a) => a.name)).toEqual(['a', 'b']);
		expect(projectAgentCount.current).toBe(2);
	});

	it("keeps the newer project's list when an older load resolves last", async () => {
		const slow = deferred<ProjectAgent[]>();
		api.getProjectAgents.mockReturnValueOnce(slow.promise).mockResolvedValueOnce([agent('mine')]);
		const first = projectAgents.loadAgents('OLD');
		await projectAgents.loadAgents('NEW');
		slow.resolve([agent('theirs')]);
		await first;
		expect(projectAgents.current.currentProjectId).toBe('NEW');
		expect(projectAgents.current.agents.map((a) => a.name)).toEqual(['mine']);
		expect(projectAgents.current.loading).toBe(false);
	});

	it('ignores a stale load failure', async () => {
		const slow = deferred<ProjectAgent[]>();
		api.getProjectAgents.mockReturnValueOnce(slow.promise).mockResolvedValueOnce([agent('mine')]);
		const first = projectAgents.loadAgents('OLD');
		await projectAgents.loadAgents('NEW');
		slow.reject({ status: 404, detail: 'Project not found' });
		await expect(first).resolves.toBeUndefined();
		expect(projectAgents.current.error).toBeNull();
	});

	it("surfaces the backend's reason when a load fails and rethrows", async () => {
		const refusal = { status: 404, detail: 'Project not found' };
		api.getProjectAgents.mockRejectedValue(refusal);
		await expect(projectAgents.loadAgents('P1')).rejects.toBe(refusal);
		expect(projectAgentsError.current).toBe('Project not found');
		expect(projectAgents.current.loading).toBe(false);
	});

	it('inserts a created agent in case-insensitive name order', async () => {
		api.getProjectAgents.mockResolvedValue([agent('alpha'), agent('Charlie')]);
		await projectAgents.loadAgents('P1');
		api.createProjectAgent.mockResolvedValue(agent('bravo'));
		await projectAgents.createAgent('P1', { name: 'bravo', description: 'd', prompt: 'p' });
		expect(projectAgents.current.agents.map((a) => a.name)).toEqual(['alpha', 'bravo', 'Charlie']);
		expect(projectAgents.current.saving).toBe(false);
	});

	it('shows the conflict detail when a create is refused', async () => {
		const conflict = { status: 409, detail: "Agent 'bravo' already exists" };
		api.createProjectAgent.mockRejectedValue(conflict);
		await expect(projectAgents.createAgent('P1', { name: 'bravo', description: 'd', prompt: 'p' })).rejects.toBe(conflict);
		expect(projectAgents.current).toMatchObject({ saving: false, error: "Agent 'bravo' already exists" });
		projectAgents.clearError();
		expect(projectAgents.current.error).toBeNull();
	});

	it('removes a deleted agent; a failed delete keeps the list', async () => {
		api.getProjectAgents.mockResolvedValue([agent('a'), agent('b')]);
		await projectAgents.loadAgents('P1');
		api.deleteProjectAgent.mockResolvedValueOnce(undefined);
		await projectAgents.deleteAgent('P1', 'a');
		expect(api.deleteProjectAgent).toHaveBeenCalledWith('P1', 'a');
		expect(projectAgents.current.agents.map((x) => x.name)).toEqual(['b']);

		api.deleteProjectAgent.mockRejectedValueOnce(new Error('disk full'));
		await expect(projectAgents.deleteAgent('P1', 'b')).rejects.toThrow('disk full');
		expect(projectAgents.current.agents.map((x) => x.name)).toEqual(['b']);
		expect(projectAgents.current).toMatchObject({ saving: false, error: 'disk full' });
	});
});
