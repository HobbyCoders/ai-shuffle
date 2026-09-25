/**
 * New-chat workspace (project) switcher: selected name and fallback, opening
 * (refreshes the cluster catalog), picking a local or remote-only project,
 * closing on Escape / outside click, Add Project, and the no-agents prompt.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';

const stores = vi.hoisted(() => ({ projects: null as never, clusterProjects: null as never, profiles: null as never }));
const loadClusterProjects = vi.hoisted(() => vi.fn());
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable } = await import('svelte/store');
	Object.assign(stores, { projects: writable([]), clusterProjects: writable([]), profiles: writable([]) });
	return {
		appData: { loadClusterProjects },
		projects: stores.projects,
		clusterProjects: stores.clusterProjects,
		profiles: stores.profiles
	};
});
vi.mock('$lib/stores/platform.svelte', () => ({ isMobileUi: () => false }));
const openFeaturePage = vi.hoisted(() => vi.fn());
vi.mock('$lib/stores/layout.svelte', () => ({ layout: { openFeaturePage } }));
vi.mock('$lib/components/AddProjectModal.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));

import WorkspaceSelector from './WorkspaceSelector.svelte';

type W = { set: (v: unknown) => void };
let onSelectProject: Mock<(projectId: string, projectV2Id?: string | null) => void>;
beforeEach(() => {
	vi.clearAllMocks();
	(stores.projects as W).set([{ id: 'p1', name: 'Website' }, { id: 'p2', name: 'API' }]);
	(stores.clusterProjects as W).set([
		{ origin: 'git@x:team/infra', name: 'Infra', project_v2_id: 'v2-infra', local_project_id: null, eligible: [{ online: true }] },
		{ origin: 'git@x:team/offline', name: 'Offline', project_v2_id: 'v2-off', local_project_id: null, eligible: [{ online: false }] }
	]);
	(stores.profiles as W).set([{ id: 'a1', name: 'Helper' }]);
	loadClusterProjects.mockResolvedValue(undefined);
	onSelectProject = vi.fn();
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function mount(selectedProject: string | null = 'p1', selectedProjectV2Id: string | null = null) {
	return render(WorkspaceSelector, { props: { selectedProject, selectedProjectV2Id, onSelectProject } });
}
const trigger = () => document.querySelector('.picker-trigger') as HTMLButtonElement;
const menu = () => document.querySelector('.picker-dropdown') as HTMLElement | null;

describe('WorkspaceSelector', () => {
	it('names the selected project, or falls back when none is chosen', () => {
		mount('p2');
		expect(trigger().textContent?.trim()).toBe('API');
		cleanup();
		mount(null);
		expect(trigger().textContent?.trim()).toBe('your project');
	});

	it('names a selected remote-only project by its catalog id', () => {
		mount(null, 'v2-infra');
		expect(trigger().textContent?.trim()).toBe('Infra');
	});

	it('opens the picker, refreshes the catalog and selects a local project', async () => {
		mount();
		expect(trigger().getAttribute('aria-expanded')).toBe('false');
		await fireEvent.click(trigger());
		expect(trigger().getAttribute('aria-expanded')).toBe('true');
		expect(trigger().getAttribute('aria-controls')).toBe(menu()!.id);
		expect(loadClusterProjects).toHaveBeenCalledOnce();
		const list = within(menu()!);
		expect(list.getByText('Website')).toBeTruthy();
		expect(list.getByText('Infra')).toBeTruthy();
		// Remote rows need at least one online install.
		expect(list.queryByText('Offline')).toBeNull();

		await fireEvent.click(list.getByText('API'));
		expect(onSelectProject).toHaveBeenCalledWith('p2', null);
		expect(menu()).toBeNull();
	});

	it('selects a remote-only project by its v2 id', async () => {
		mount();
		await fireEvent.click(trigger());
		await fireEvent.click(within(menu()!).getByText('Infra'));
		expect(onSelectProject).toHaveBeenCalledWith('', 'v2-infra');
	});

	it('closes on Escape (focus back on the trigger) and on an outside click', async () => {
		mount();
		await fireEvent.click(trigger());
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(menu()).toBeNull();
		expect(document.activeElement).toBe(trigger());

		await fireEvent.click(trigger());
		await fireEvent.click(document.body);
		expect(menu()).toBeNull();
		expect(onSelectProject).not.toHaveBeenCalled();
	});

	it('closes the picker to add a project', async () => {
		mount();
		await fireEvent.click(trigger());
		await fireEvent.click(within(menu()!).getByRole('button', { name: /Add Project/ }));
		expect(menu()).toBeNull();
	});

	it('points a fresh install without agents at the agent editor', async () => {
		(stores.profiles as W).set([]);
		mount();
		await fireEvent.click(screen.getByRole('button', { name: 'No agents yet — create one to start chatting' }));
		expect(openFeaturePage).toHaveBeenCalledWith('profile');
	});
});
