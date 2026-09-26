/**
 * Project tabs and the hooks card start their store loads from an effect. The
 * stores record the failure and rethrow (callers such as ProjectMemoryTab's
 * "changed elsewhere" check rely on that), so each component must catch the
 * effect's promise: a backend failure shows the error, never an unhandled
 * rejection.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
	getProjectAgents: vi.fn(),
	getMemoryFiles: vi.fn(),
	getProjectHooks: vi.fn(),
	getPluginHooks: vi.fn(),
	getProjectPluginHooks: vi.fn(),
	getAppHooks: vi.fn()
}));
vi.mock('$lib/api/projectAgents', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/projectAgents')>()),
	getProjectAgents: api.getProjectAgents
}));
vi.mock('$lib/api/projectMemory', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/projectMemory')>()),
	getMemoryFiles: api.getMemoryFiles
}));
vi.mock('$lib/api/client', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/client')>()),
	getProjectHooks: api.getProjectHooks,
	getPluginHooks: api.getPluginHooks,
	getProjectPluginHooks: api.getProjectPluginHooks,
	getAppHooks: api.getAppHooks
}));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));

import { projectAgents } from '$lib/stores/projectAgents.svelte';
import { projectMemory } from '$lib/stores/projectMemory.svelte';
import { hooks } from '$lib/stores/hooks.svelte';
import ProjectAgentsTab from './ProjectAgentsTab.svelte';
import ProjectMemoryTab from './ProjectMemoryTab.svelte';
import ProjectHooksTab from './ProjectHooksTab.svelte';
import HooksCard from './HooksCard.svelte';

const unhandled = vi.fn();
beforeEach(() => {
	vi.clearAllMocks();
	projectAgents.reset();
	projectMemory.reset();
	hooks.reset();
	for (const fn of Object.values(api)) fn.mockRejectedValue(new Error('backend offline'));
	process.on('unhandledRejection', unhandled);
});
afterEach(async () => {
	// Give a stray rejection a turn to surface before checking.
	await new Promise((r) => setTimeout(r, 0));
	process.off('unhandledRejection', unhandled);
	cleanup();
	expect(unhandled).not.toHaveBeenCalled();
});

describe('a rejected store load is caught and shown', () => {
	it('ProjectAgentsTab', async () => {
		render(ProjectAgentsTab, { props: { projectId: 'p1' } });
		expect(await screen.findByText('backend offline')).toBeTruthy();
		expect(api.getProjectAgents).toHaveBeenCalledWith('p1');
	});

	it('ProjectMemoryTab', async () => {
		render(ProjectMemoryTab, { props: { projectId: 'p1' } });
		expect(await screen.findByText('backend offline')).toBeTruthy();
		expect(api.getMemoryFiles).toHaveBeenCalledWith('p1');
	});

	it('ProjectHooksTab', async () => {
		render(ProjectHooksTab, { props: { projectId: 'p1' } });
		expect(await screen.findByText('backend offline')).toBeTruthy();
		expect(api.getProjectHooks).toHaveBeenCalledWith('p1');
	});

	it('HooksCard (app hooks, no project)', async () => {
		render(HooksCard, { props: { embedded: true } });
		expect(await screen.findByText('backend offline')).toBeTruthy();
		expect(api.getAppHooks).toHaveBeenCalled();
	});
});
