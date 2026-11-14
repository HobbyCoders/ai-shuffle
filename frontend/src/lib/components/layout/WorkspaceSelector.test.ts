/**
 * New-chat workspace (project) switcher: selected name and fallback, opening
 * (refreshes the cluster catalog), picking a local or remote-only project,
 * closing on Escape / outside click, Add Project, and the no-agents prompt.
 * The new-session row's `sentence` picker: search, most recently used first,
 * ↑/↓ + Enter, digit keys, a "No project" row while one is picked, and focus
 * back on the message editor when Cmd/Ctrl+P opened it.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { tick } from 'svelte';

const stores = vi.hoisted(() => ({ projects: null as never, clusterProjects: null as never, profiles: null as never, profilesLoaded: null as never, sessions: null as never }));
const loadClusterProjects = vi.hoisted(() => vi.fn());
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable } = await import('svelte/store');
	Object.assign(stores, { projects: writable([]), clusterProjects: writable([]), profiles: writable([]), profilesLoaded: writable(true), sessions: writable([]) });
	return {
		appData: { loadClusterProjects },
		projects: stores.projects,
		clusterProjects: stores.clusterProjects,
		profiles: stores.profiles,
		profilesLoaded: stores.profilesLoaded,
		sessions: stores.sessions
	};
});
vi.mock('$lib/stores/platform.svelte', () => ({ isMobileUi: () => false }));
const openFeaturePage = vi.hoisted(() => vi.fn());
vi.mock('$lib/stores/layout.svelte', () => ({ layout: { openFeaturePage } }));
vi.mock('$lib/components/AddProjectModal.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));

import WorkspaceSelector from './WorkspaceSelector.svelte';
import { groups } from '$lib/stores/groups.svelte';

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
	(stores.profilesLoaded as W).set(true);
	(stores.sessions as W).set([]);
	loadClusterProjects.mockResolvedValue(undefined);
	onSelectProject = vi.fn();
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function mount(selectedProject: string | null = 'p1', selectedProjectV2Id: string | null = null) {
	return render(WorkspaceSelector, { props: { selectedProject, selectedProjectV2Id, onSelectProject } });
}
const trigger = () => screen.getByRole('button', { name: /^(Switch project, current: |Choose a project$)/ }) as HTMLButtonElement;
// The dropdown is the element the trigger says it controls (absent while closed).
const menu = () => document.getElementById(trigger().getAttribute('aria-controls') ?? '');

describe('WorkspaceSelector', () => {
	it('names the switcher by its purpose and the selected project, or falls back when none is chosen', () => {
		mount('p2');
		const named = screen.getByRole('button', { name: 'Switch project, current: API' });
		expect(named.textContent?.trim()).toBe('API');
		cleanup();
		mount(null);
		const unnamed = screen.getByRole('button', { name: 'Choose a project' });
		expect(unnamed.textContent?.trim()).toBe('your project');
	});

	it('names a selected remote-only project by its catalog id', () => {
		mount(null, 'v2-infra');
		expect(screen.getByRole('button', { name: 'Switch project, current: Infra' }).textContent?.trim()).toBe('Infra');
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

	it('says nothing about agents while the roster is still loading', () => {
		(stores.profiles as W).set([]);
		(stores.profilesLoaded as W).set(false);
		mount();
		expect(screen.queryByRole('button', { name: /No agents yet/ })).toBeNull();
	});
});

describe('WorkspaceSelector sentence picker', () => {
	const at = (iso: string, project_id: string) => ({ project_id, updated_at: iso, created_at: iso });
	let onClearProject: Mock<() => void>;
	let editor: HTMLElement;
	let selector: { openPicker: (o?: { anchor?: HTMLElement | null; returnFocus?: HTMLElement | null }) => void };

	beforeEach(() => {
		onClearProject = vi.fn();
		(stores.projects as W).set([{ id: 'p1', name: 'Website' }, { id: 'p2', name: 'API' }, { id: 'p3', name: 'Docs site' }]);
		(stores.clusterProjects as W).set([]);
		// API was used last, then Docs site; Website never.
		(stores.sessions as W).set([at('2026-11-01T10:00:00Z', 'p3'), at('2026-11-02T10:00:00Z', 'p2'), at('2026-10-01T10:00:00Z', 'p3')]);
		editor = document.createElement('div');
		editor.tabIndex = 0;
		editor.setAttribute('data-message-editor', '');
		document.body.append(editor);
	});
	afterEach(() => editor.remove());

	function mountSentence(selectedProject: string | null = null, headless = false) {
		const view = render(WorkspaceSelector, { props: { variant: 'sentence', headless, selectedProject, onSelectProject, onClearProject } });
		selector = view.component as unknown as typeof selector;
		return view;
	}
	const dialog = () => screen.getByRole('dialog');
	const search = () => within(dialog()).getByRole('textbox', { name: 'Search projects' }) as HTMLInputElement;
	const rowNames = () => [...dialog().querySelectorAll<HTMLElement>('.panel-scroll .menu-item')].map(row => row.querySelector('.item-name')?.textContent);
	const highlighted = () => dialog().querySelector('.panel-scroll [data-highlighted] .item-name')?.textContent;

	async function openFromEditor(selectedProject: string | null = null, headless = false) {
		mountSentence(selectedProject, headless);
		editor.focus();
		selector.openPicker({ returnFocus: editor });
		await tick();
	}

	it('names its shortcut on the No project word', () => {
		mountSentence();
		const word = screen.getByRole('button', { name: /^No project/ });
		expect(word.getAttribute('aria-keyshortcuts')).toMatch(/^(Control|Meta)\+P$/);
		expect(word.title).toMatch(/(Ctrl\+P|⌘P)$/);
	});

	it('opens from the keyboard with the search focused, most recently used first, numbered', async () => {
		await openFromEditor();
		expect(document.activeElement).toBe(search());
		expect(rowNames()).toEqual(['API', 'Docs site', 'Website']);
		expect([...dialog().querySelectorAll('.panel-scroll .row-key')].map(k => k.textContent)).toEqual(['1', '2', '3']);
		expect(highlighted()).toBe('API');
		expect(dialog().querySelector('.shortcut-hint')?.textContent).toMatch(/^(Ctrl\+P|⌘P)$/);
		expect(loadClusterProjects).toHaveBeenCalledOnce();
	});

	it('filters as you type, best match first, and Enter picks the top match back into the editor', async () => {
		await openFromEditor();
		await fireEvent.input(search(), { target: { value: 'site' } });
		// "Docs site" has it at a word start; "Website" only inside a word.
		expect(rowNames()).toEqual(['Docs site', 'Website']);
		await fireEvent.keyDown(search(), { key: 'Enter' });
		expect(onSelectProject).toHaveBeenCalledWith('p3', null);
		expect(screen.queryByRole('dialog')).toBeNull();
		expect(document.activeElement).toBe(editor);
	});

	it('moves the highlight with the arrow keys, wrapping', async () => {
		await openFromEditor();
		await fireEvent.keyDown(search(), { key: 'ArrowDown' });
		expect(highlighted()).toBe('Docs site');
		await fireEvent.keyDown(search(), { key: 'ArrowUp' });
		await fireEvent.keyDown(search(), { key: 'ArrowUp' });
		expect(highlighted()).toBe('Website');
		await fireEvent.keyDown(search(), { key: 'Enter' });
		expect(onSelectProject).toHaveBeenCalledWith('p1', null);
	});

	it('picks a row by its digit while the search is empty', async () => {
		await openFromEditor();
		const two = new KeyboardEvent('keydown', { key: '2', bubbles: true, cancelable: true });
		search().dispatchEvent(two);
		expect(two.defaultPrevented).toBe(true);
		expect(onSelectProject).toHaveBeenCalledWith('p3', null);
	});

	it('lets a digit type once the search has text, or when no row has it', async () => {
		await openFromEditor();
		const nine = new KeyboardEvent('keydown', { key: '9', bubbles: true, cancelable: true });
		search().dispatchEvent(nine);
		expect(nine.defaultPrevented).toBe(false);
		await fireEvent.input(search(), { target: { value: 'a' } });
		const one = new KeyboardEvent('keydown', { key: '1', bubbles: true, cancelable: true });
		search().dispatchEvent(one);
		expect(one.defaultPrevented).toBe(false);
		expect(onSelectProject).not.toHaveBeenCalled();
	});

	it('closes on Escape with focus back in the editor', async () => {
		await openFromEditor();
		await fireEvent.keyDown(search(), { key: 'Escape' });
		expect(screen.queryByRole('dialog')).toBeNull();
		expect(document.activeElement).toBe(editor);
		expect(onSelectProject).not.toHaveBeenCalled();
	});

	it('switches a picked project from the project word, with a No project row (0)', async () => {
		const anchor = document.createElement('button');
		document.body.append(anchor);
		await openFromEditor('p2', true);
		expect(screen.queryByRole('button', { name: /^No project\. Add a project/ })).toBeNull();
		expect(screen.getByRole('dialog', { name: 'Switch project' })).toBeTruthy();
		expect(rowNames()).toEqual(['API', 'Docs site', 'Website', 'No project']);
		// Picking the current project changes nothing.
		await fireEvent.keyDown(search(), { key: 'Enter' });
		expect(onSelectProject).not.toHaveBeenCalled();
		selector.openPicker({ anchor, returnFocus: editor });
		await tick();
		search().dispatchEvent(new KeyboardEvent('keydown', { key: '0', bubbles: true, cancelable: true }));
		expect(onClearProject).toHaveBeenCalledOnce();
		expect(onSelectProject).not.toHaveBeenCalled();
		expect(document.activeElement).toBe(editor);
		anchor.remove();
	});

	it('keeps the place focus goes back to when the shortcut is pressed again while open', async () => {
		await openFromEditor();
		// The second press comes from the search field: no editor to return to.
		selector.openPicker({ returnFocus: null });
		await tick();
		expect(document.activeElement).toBe(search());
		await fireEvent.keyDown(search(), { key: 'Escape' });
		expect(document.activeElement).toBe(editor);
		// Closed, then opened again: it takes the new place.
		const other = document.createElement('button');
		document.body.append(other);
		other.focus();
		selector.openPicker({ returnFocus: other });
		await tick();
		await fireEvent.keyDown(search(), { key: '1' });
		expect(onSelectProject).toHaveBeenCalledWith('p2', null);
		expect(document.activeElement).toBe(other);
		other.remove();
	});

	describe('with every project in a group', () => {
		beforeEach(() => {
			groups.assignToGroup('projects', 'p1', 'Work');
			groups.assignToGroup('projects', 'p2', 'Work');
			groups.assignToGroup('projects', 'p3', 'Side');
		});
		afterEach(() => {
			groups.deleteGroup('projects', 'Work');
			groups.deleteGroup('projects', 'Side');
		});

		it('lists them flat, most recently used first and numbered', async () => {
			await openFromEditor();
			expect(dialog().querySelector('.group-item')).toBeNull();
			expect(rowNames()).toEqual(['API', 'Docs site', 'Website']);
			expect([...dialog().querySelectorAll('.panel-scroll .row-key')].map(k => k.textContent)).toEqual(['1', '2', '3']);
			await fireEvent.input(search(), { target: { value: 'site' } });
			expect(rowNames()).toEqual(['Docs site', 'Website']);
			await fireEvent.input(search(), { target: { value: '' } });
			const one = new KeyboardEvent('keydown', { key: '1', bubbles: true, cancelable: true });
			search().dispatchEvent(one);
			expect(one.defaultPrevented).toBe(true);
			expect(onSelectProject).toHaveBeenCalledWith('p2', null);
		});

		it('still offers No project (0) and Add Project when switching', async () => {
			await openFromEditor('p2', true);
			expect(rowNames()).toEqual(['API', 'Docs site', 'Website', 'No project']);
			expect(within(dialog()).getByRole('button', { name: /Add Project/ })).toBeTruthy();
			search().dispatchEvent(new KeyboardEvent('keydown', { key: '0', bubbles: true, cancelable: true }));
			expect(onClearProject).toHaveBeenCalledOnce();
		});

		it('keeps the groups in the phone sheet list', async () => {
			render(WorkspaceSelector, { props: { variant: 'sentence', sheetMode: true, selectedProject: null, onSelectProject } });
			const word = screen.getByRole('button', { name: /^No project/ });
			await fireEvent.click(word);
			const list = document.getElementById(word.getAttribute('aria-controls') ?? '')!;
			expect([...list.querySelectorAll('.group-item .item-name')].map(n => n.textContent)).toEqual(['Work', 'Side']);
			expect(list.querySelector('.row-key')).toBeNull();
		});
	});

	it('gives a mouse click the same search, keeping Add Project', async () => {
		vi.stubGlobal('matchMedia', () => ({ matches: true }));
		mountSentence();
		await fireEvent.click(screen.getByRole('button', { name: /^No project/ }));
		await tick();
		expect(document.activeElement).toBe(search());
		await fireEvent.input(search(), { target: { value: 'zzz' } });
		expect(within(dialog()).getByText('No matches')).toBeTruthy();
		expect(within(dialog()).getByRole('button', { name: /Add Project/ })).toBeTruthy();
	});

	it('keeps the phone sheet list plain: no search, numbers or shortcut', async () => {
		render(WorkspaceSelector, { props: { variant: 'sentence', sheetMode: true, selectedProject: null, onSelectProject } });
		const word = screen.getByRole('button', { name: /^No project/ });
		expect(word.hasAttribute('aria-keyshortcuts')).toBe(false);
		await fireEvent.click(word);
		const list = document.getElementById(word.getAttribute('aria-controls') ?? '')!;
		expect(within(list).queryByRole('textbox')).toBeNull();
		expect(list.querySelector('.row-key, .shortcut-hint')).toBeNull();
		// Catalog order, as before.
		expect([...list.querySelectorAll('.menu-item .item-name')].map(n => n.textContent)).toEqual(['Website', 'API', 'Docs site', 'Add Project']);
	});
});
