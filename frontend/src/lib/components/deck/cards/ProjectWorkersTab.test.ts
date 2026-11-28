/**
 * Workers tab: one row per worker with its status, folder and branch. A row
 * opens that worker's details under it (the same at every width), where its
 * folder is changed and its copy removed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));
vi.mock('$lib/stores/app-data.svelte', () => ({ appData: { loadProjectCatalog: vi.fn(), deleteProject: vi.fn() } }));
vi.mock('$lib/stores/projects-overview', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/projects-overview')>()),
	patchOverviewWorker: vi.fn()
}));
vi.mock('$lib/stores/auth.svelte', async () => ({ isAdmin: (await import('svelte/store')).writable(true) }));
vi.mock('$lib/stores/layout.svelte', () => ({ layout: { openChat: vi.fn(), openFeaturePage: vi.fn() } }));
vi.mock('$lib/stores/active-session', () => ({ activeSession: { newChat: vi.fn(), setProject: vi.fn(), setDeviceIntent: vi.fn() } }));
vi.mock('$lib/api/project-workers', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/project-workers')>()),
	getProjectWorkers: vi.fn(),
	startProjectSetup: vi.fn(),
	cancelProjectSetup: vi.fn(),
	changeWorkerFolder: vi.fn(),
	getProjectRemovalPreview: vi.fn()
}));
import ProjectWorkersTab from './ProjectWorkersTab.svelte';
import { cancelProjectSetup, changeWorkerFolder, getProjectRemovalPreview, getProjectWorkers, startProjectSetup } from '$lib/api/project-workers';
import { watchResources } from '$lib/services/watch-resources';
import { appData } from '$lib/stores/app-data.svelte';

const worker = (install_id: string, device_name: string, state: string, extra: Record<string, unknown> = {}) => ({
	install_id, device_name, is_self: install_id === 'self', online: true, approved: true, state, setup: null,
	folder: state === 'ready' ? `/workspace/example/${install_id}/demo` : null, suggested_folder: 'demo', branch: state === 'ready' ? 'main' : null,
	dirty: false, changed_files: 0, last_seen: null, folder_revision: state === 'ready' ? 'rev-1' : null, ...extra
});
const response = (workers: ReturnType<typeof worker>[], remote = 'github.com/example/demo') => ({
	project_v2_id: 'pv2-demo', name: 'Demo', kind: 'git' as const, remote, default_folder: 'demo', workers
});
const ref = { project_v2_id: 'pv2-demo' };
/** A worker's row button: its name starts with the worker's name. */
const rowButton = (name: string) => screen.findByRole('button', { name: new RegExp(`^${name}`) }).then((b) => b as HTMLButtonElement);
const openRow = async (name: string) => fireEvent.click(await rowButton(name));
const details = (name: string) => screen.getByRole('region', { name: `${name} details` });
const items = () => [...document.querySelectorAll<HTMLElement>('.pw-item')];
/** The open details (at most one worker's). */
const openDetails = () => [...document.querySelectorAll<HTMLElement>('.pw-card')];

beforeEach(() => {
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

describe('Worker rows', () => {
	const mixed = response([
		worker('self', 'Home server', 'ready', { changed_files: 2, dirty: true }),
		worker('desk', 'Desktop', 'setting_up', { setup: { op_id: 'op-1', status: 'running', progress: 40, error: null } }),
		worker('lap', 'Laptop', 'failed', { setup: { op_id: 'op-2', status: 'error', progress: null, error: 'Folder exists.' } }),
		worker('nas', 'Storage box', 'queued_offline', { online: false, setup: { op_id: 'op-3', status: 'queued', progress: null, error: null } }),
		worker('spare', 'Spare', 'missing')
	]);

	it('shows one row per worker with its status, folder and branch, and no editable path', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(mixed);
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		expect(await screen.findByRole('heading', { name: /Workers/ })).toBeTruthy();
		const rows = items();
		expect(rows.map((r) => r.getAttribute('data-worker-state'))).toEqual(['ready', 'setting_up', 'failed', 'queued_offline', 'missing']);
		// No table, side panel or sheet at any width: rows of one card.
		expect(screen.queryByRole('table')).toBeNull();
		expect(screen.queryByRole('complementary')).toBeNull();

		expect(rows[0].textContent).toContain('This worker');
		expect(rows[0].textContent).toContain('Online');
		expect(rows[0].textContent).toContain('Ready');
		expect(rows[0].querySelector('.pw-row-folder [title]')?.getAttribute('title')).toBe('/workspace/example/self/demo');
		expect(rows[0].textContent).toContain('main');
		expect(rows[1].textContent).toContain('Cloning 40%');
		expect(rows[2].textContent).toContain('Failed');
		expect(within(rows[2]).getByRole('button', { name: 'Retry' })).toBeTruthy();
		expect(rows[3].textContent).toContain('Offline');
		expect(rows[3].textContent).toContain('Queued until online');
		expect(rows[4].textContent).toContain('Not set up');
		expect(within(rows[4]).getByRole('button', { name: 'Set up' })).toBeTruthy();

		// Every row starts closed; the folder editor lives in a worker's details.
		expect(openDetails()).toHaveLength(0);
		expect(screen.queryByPlaceholderText('Project path')).toBeNull();
		expect(screen.queryByRole('button', { name: 'Change folder' })).toBeNull();
	});

	it('says once that a remote-only project is not here yet and sets it up here', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(response([worker('self', 'Home server', 'missing'), worker('desk', 'Desktop', 'ready')]));
		vi.mocked(startProjectSetup).mockResolvedValue({ op_id: 'op-1', install_id: 'self', status: 'queued' } as never);
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: null });

		expect(await screen.findByText(/Demo isn't on Home server yet\. Set it up here/)).toBeTruthy();
		expect(screen.queryByText(/have this project yet/)).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Set up here' }));
		expect(vi.mocked(startProjectSetup).mock.calls[0][1]).toMatchObject({ install_id: 'self' });
	});

	it('names every online worker without the project when there are several', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(response([worker('self', 'Home server', 'missing'), worker('desk', 'Desktop', 'missing')]));
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: null });

		expect(await screen.findByText(/Home server and Desktop don't have this project yet/)).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Set up on all missing' })).toBeTruthy();
	});

	it('opens a worker under its row, one at a time, and the same row closes it', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(mixed);
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		const row = await rowButton('Home server');
		expect(row.getAttribute('aria-expanded')).toBe('false');
		row.focus();
		await fireEvent.click(row);
		const open = details('Home server');
		expect(row.getAttribute('aria-expanded')).toBe('true');
		expect(row.getAttribute('aria-controls')).toBe(open.id);
		// The details sit inside the row's own item, not beside the list.
		expect(open.closest('.pw-item')).toBe(row.closest('.pw-item'));
		expect(within(open).getByRole('button', { name: 'New chat here' })).toBeTruthy();
		// The row that was pressed keeps the keyboard.
		await new Promise((r) => setTimeout(r, 0));
		expect(document.activeElement).toBe(row);

		await fireEvent.click(within(open).getByRole('button', { name: 'Change folder' }));
		expect(within(open).getByRole('textbox', { name: 'New folder on Home server' })).toBeTruthy();
		expect(within(open).getByRole('button', { name: 'Browse' })).toBeTruthy();

		await openRow('Laptop');
		expect(openDetails()).toHaveLength(1);
		expect(details('Laptop')).toBeTruthy();
		expect(row.getAttribute('aria-expanded')).toBe('false');

		await openRow('Laptop');
		expect(openDetails()).toHaveLength(0);
	});

	it('opens and closes a row from anywhere on it', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(mixed);
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await rowButton('Home server');
		await fireEvent.click(items()[0].querySelector('.pw-status')!);
		expect(details('Home server')).toBeTruthy();
		await fireEvent.click(items()[0].querySelector('.pw-status')!);
		expect(openDetails()).toHaveLength(0);
	});

	it('explains a failure, a running setup and a queued one in the details', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(mixed);
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await openRow('Desktop');
		let open = details('Desktop');
		expect(within(open).getByRole('progressbar', { name: 'Setting up on Desktop' })).toBeTruthy();
		expect(open.textContent).toContain('You can leave this page');
		expect(within(open).getByRole('button', { name: 'Cancel setup' })).toBeTruthy();

		await openRow('Laptop');
		open = details('Laptop');
		expect(open.textContent).toContain('Folder exists.');
		expect(within(open).getByRole('button', { name: 'Retry' })).toBeTruthy();

		await openRow('Storage box');
		open = details('Storage box');
		expect(open.textContent).toContain('It sets up when Storage box comes back online.');
		expect(within(open).getByRole('button', { name: 'Withdraw' })).toBeTruthy();
	});

	it('cancels a running setup from its details', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(mixed);
		vi.mocked(cancelProjectSetup).mockResolvedValue(undefined as never);
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await openRow('Desktop');
		await fireEvent.click(within(details('Desktop')).getByRole('button', { name: 'Cancel setup' }));
		expect(cancelProjectSetup).toHaveBeenCalledWith('op-1');
	});

	it('refuses to save a folder change once the registered folder has changed underneath it', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(mixed);
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await openRow('Home server');
		const open = details('Home server');
		await fireEvent.click(within(open).getByRole('button', { name: 'Change folder' }));
		await fireEvent.input(within(open).getByRole('textbox', { name: 'New folder on Home server' }), { target: { value: '/workspace/example/other' } });

		// Another device moved it meanwhile: the next read brings a new revision.
		const moved = { ...mixed, workers: mixed.workers.map((w) => (w.install_id === 'self' ? { ...w, folder: '/workspace/example/moved', folder_revision: 'rev-2' } : w)) };
		vi.mocked(getProjectWorkers).mockResolvedValue(moved);
		await vi.mocked(watchResources).mock.calls[0][1]();

		expect(await within(open).findByRole('alert')).toHaveProperty('textContent', 'The registered folder changed. Cancel and reopen to edit the current folder.');
		const save = within(open).getByRole('button', { name: 'Save folder' }) as HTMLButtonElement;
		expect(save.disabled).toBe(true);
		await fireEvent.submit(save.closest('form')!);
		expect(changeWorkerFolder).not.toHaveBeenCalled();
	});

	it('sets a missing worker up from its row', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(mixed);
		vi.mocked(startProjectSetup).mockResolvedValue({ op_id: 'op-9', status: 'running' } as never);
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await rowButton('Spare');
		const row = items().find((r) => r.getAttribute('data-worker-state') === 'missing')!;
		await fireEvent.click(within(row).getByRole('button', { name: 'Set up' }));
		// The default folder, as suggested; the worker opens so its progress shows.
		expect(startProjectSetup).toHaveBeenCalledWith(ref, { install_id: 'spare', folder: 'demo', mode: 'auto' });
		expect(details('Spare')).toBeTruthy();
	});

	it('starts a chat on a worker from its details', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(mixed);
		const { layout } = await import('$lib/stores/layout.svelte');
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await openRow('Home server');
		await fireEvent.click(within(details('Home server')).getByRole('button', { name: 'New chat here' }));
		expect(layout.openChat).toHaveBeenCalledWith(null);
	});

	it('sends This worker’s GitHub sign-in to Settings; a remote worker’s help stays in its details', async () => {
		const githubFailed = (id: string, name: string) => worker(id, name, 'failed', { setup: { op_id: `op-${id}`, status: 'error', progress: null, error: null, error_code: 'github_auth' } });
		vi.mocked(getProjectWorkers).mockResolvedValue(response([githubFailed('self', 'Home server'), githubFailed('desk', 'Desktop')]));
		const { layout } = await import('$lib/stores/layout.svelte');
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await openRow('Home server');
		await fireEvent.click(within(details('Home server')).getByRole('button', { name: 'Sign in to GitHub on Home server' }));
		expect(layout.openFeaturePage).toHaveBeenCalledWith('settings', 'category=integrations&item=github');

		// Another worker's sign-in can't be done from here: its help shows in its details.
		await openRow('Desktop');
		const remote = details('Desktop');
		await fireEvent.click(within(remote).getByRole('button', { name: 'Sign in to GitHub on Desktop' }));
		expect(within(remote).getByText(/On Desktop, open AI Shuffle, go to Settings → Integrations → GitHub/)).toBeTruthy();
		expect(layout.openFeaturePage).toHaveBeenCalledTimes(1);
	});

	it('shows a failed Retry in that worker’s open details, not on the page as well', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(mixed);
		vi.mocked(startProjectSetup).mockRejectedValue({ status: 409, detail: 'Folder exists.' });
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await openRow('Laptop');
		const open = details('Laptop');
		await fireEvent.click(within(open).getByRole('button', { name: 'Retry' }));

		expect(await within(open).findByText(/Couldn't start the setup on Laptop: Folder exists\./)).toBeTruthy();
		expect(screen.getAllByText(/Couldn't start the setup on Laptop/)).toHaveLength(1);
	});

	it('keeps the page banner for a failure the page started (Set up on all missing)', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(response([worker('self', 'Home server', 'ready'), worker('spare', 'Spare', 'missing'), worker('desk', 'Desktop', 'missing')]));
		vi.mocked(startProjectSetup).mockImplementation(async (_ref, opts) => {
			if (opts.install_id === 'spare') throw { status: 502, detail: 'Spare did not answer' };
			return { op_id: 'op-9', status: 'running' } as never;
		});
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await fireEvent.click(await screen.findByRole('button', { name: 'Set up on all missing' }));
		const message = await screen.findByText(/Couldn't start the setup on Spare: Spare did not answer/);
		expect(message.closest('.pw-card')).toBeNull();
	});
});

describe('The Settings tab asks for this worker’s removal', () => {
	it('opens this worker’s details with its removal already counting, once', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(response([worker('self', 'Here', 'ready'), worker('peer', 'Tower', 'ready')]));
		vi.mocked(getProjectRemovalPreview).mockResolvedValue({ project_id: 'demo', chats: 2, worktrees: [] });
		const onSelfRemovalAsked = vi.fn();
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo', askSelfRemoval: true, onSelfRemovalAsked });

		const open = await screen.findByRole('region', { name: 'Here details' });
		expect(await within(open).findByRole('button', { name: 'Remove' })).toBeTruthy();
		expect(getProjectRemovalPreview).toHaveBeenCalledWith('demo');
		expect(onSelfRemovalAsked).toHaveBeenCalledOnce();
		// The page opened these details, so they take the keyboard.
		await waitFor(() => expect(open.contains(document.activeElement)).toBe(true));
	});

	it('asks again with a fresh count when Settings sends the owner back after Cancel', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(response([worker('self', 'Here', 'ready'), worker('peer', 'Tower', 'ready')]));
		vi.mocked(getProjectRemovalPreview)
			.mockResolvedValueOnce({ project_id: 'demo', chats: 2, worktrees: [] })
			.mockResolvedValueOnce({ project_id: 'demo', chats: 3, worktrees: [] });
		const { rerender } = render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo', askSelfRemoval: true });

		const open = await screen.findByRole('region', { name: 'Here details' });
		expect(await within(open).findByText(/its 2 chats\?/)).toBeTruthy();
		await fireEvent.click(within(open).getByRole('button', { name: 'Cancel' }));
		expect(within(open).queryByRole('button', { name: 'Remove' })).toBeNull();

		// The parent clears its request once taken; the owner asks from Settings again.
		await rerender({ askSelfRemoval: false });
		await rerender({ askSelfRemoval: true });
		expect(await within(open).findByText(/its 3 chats\?/)).toBeTruthy();
		expect(getProjectRemovalPreview).toHaveBeenCalledTimes(2);
	});

	it('does not ask again after Cancel when the details close and reopen', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(response([worker('self', 'Here', 'ready'), worker('peer', 'Tower', 'ready')]));
		vi.mocked(getProjectRemovalPreview).mockResolvedValue({ project_id: 'demo', chats: 2, worktrees: [] });
		const { rerender } = render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo', askSelfRemoval: true });

		const open = await screen.findByRole('region', { name: 'Here details' });
		expect(await within(open).findByText(/its 2 chats\?/)).toBeTruthy();
		await fireEvent.click(within(open).getByRole('button', { name: 'Cancel' }));
		await rerender({ askSelfRemoval: false });

		await openRow('Here');
		expect(openDetails()).toHaveLength(0);
		await openRow('Here');
		const reopened = details('Here');
		expect(within(reopened).getByRole('button', { name: 'Remove project and chats' })).toBeTruthy();
		expect(within(reopened).queryByRole('button', { name: 'Remove' })).toBeNull();
		expect(getProjectRemovalPreview).toHaveBeenCalledOnce();
	});
});

describe('This worker’s removal when its details close and reopen', () => {
	const self = () => response([worker('self', 'Here', 'ready'), worker('peer', 'Tower', 'ready')]);
	// clearAllMocks keeps queued once-answers; a failed test must not hand them to the next.
	beforeEach(() => { vi.mocked(getProjectRemovalPreview).mockReset(); vi.mocked(appData.deleteProject).mockReset(); });

	it('forgets an open confirmation when the details close, or another worker opens', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(self());
		vi.mocked(getProjectRemovalPreview).mockResolvedValue({ project_id: 'demo', chats: 2, worktrees: [] });
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await openRow('Here');
		await fireEvent.click(within(details('Here')).getByRole('button', { name: 'Remove project and chats' }));
		expect(await within(details('Here')).findByText(/its 2 chats\?/)).toBeTruthy();
		await openRow('Here');

		await openRow('Here');
		expect(within(details('Here')).getByRole('button', { name: 'Remove project and chats' })).toBeTruthy();
		expect(getProjectRemovalPreview).toHaveBeenCalledOnce();

		await fireEvent.click(within(details('Here')).getByRole('button', { name: 'Remove project and chats' }));
		expect(await within(details('Here')).findByText(/its 2 chats\?/)).toBeTruthy();
		await openRow('Tower');
		await openRow('Here');
		expect(within(details('Here')).getByRole('button', { name: 'Remove project and chats' })).toBeTruthy();
		expect(within(details('Here')).queryByRole('button', { name: 'Remove' })).toBeNull();
		expect(getProjectRemovalPreview).toHaveBeenCalledTimes(2);
	});

	it('keeps a running removal running while its details are closed: no second request, and its failure shows in the reopened row', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(self());
		vi.mocked(getProjectRemovalPreview).mockResolvedValue({ project_id: 'demo', chats: 2, worktrees: [] });
		let fail: (e: unknown) => void = () => {};
		vi.mocked(appData.deleteProject).mockImplementation(() => new Promise((_, reject) => { fail = reject; }));
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await openRow('Here');
		await fireEvent.click(within(details('Here')).getByRole('button', { name: 'Remove project and chats' }));
		expect(await within(details('Here')).findByText(/its 2 chats\?/)).toBeTruthy();
		await fireEvent.click(within(details('Here')).getByRole('button', { name: 'Remove' }));
		expect(appData.deleteProject).toHaveBeenCalledOnce();

		// Closed and reopened while the DELETE runs: removal cannot be asked again.
		await openRow('Here');
		await openRow('Here');
		const again = within(details('Here')).getByRole('button', { name: 'Remove project and chats' }) as HTMLButtonElement;
		expect(again.disabled).toBe(true);
		expect(appData.deleteProject).toHaveBeenCalledOnce();
		expect(getProjectRemovalPreview).toHaveBeenCalledOnce();

		// The request fails: the row the owner is looking at says so, and offers it again.
		fail({ detail: 'The project is busy.' });
		expect((await within(details('Here')).findByRole('alert')).textContent).toBe('The project is busy.');
		expect((within(details('Here')).getByRole('button', { name: 'Remove project and chats' }) as HTMLButtonElement).disabled).toBe(false);
		expect(appData.deleteProject).toHaveBeenCalledOnce();
	});
});

describe('Another local checkout of the same origin', () => {
	// Two folders on this worker cloned from one origin share a logical project:
	// read by the second one's local id, the answer describes the first one.
	const firstCheckout = { ...response([worker('self', 'Home server', 'ready'), worker('desk', 'Desktop', 'ready')]), local_project_id: 'demo' };

	it('shows no worker controls for the other checkout and hands its folder to Settings', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(firstCheckout as never);
		const onUnavailable = vi.fn();
		const onLoaded = vi.fn();
		render(ProjectWorkersTab, { projectRef: { project_id: 'demo-copy' }, projectName: 'Demo copy', localProjectId: 'demo-copy', expectLocalId: 'demo-copy', onUnavailable, onLoaded });

		await waitFor(() => expect(onUnavailable).toHaveBeenCalledOnce());
		expect(onLoaded).not.toHaveBeenCalled();
		expect(screen.queryByRole('button', { name: /^Home server/ })).toBeNull();
		expect(items()).toHaveLength(0);
		expect(screen.queryByText('/workspace/example/self/demo')).toBeNull();
		expect(changeWorkerFolder).not.toHaveBeenCalled();
	});

	it('shows the worker rows when the answer names the checkout it was read for', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(firstCheckout as never);
		const onUnavailable = vi.fn();
		render(ProjectWorkersTab, { projectRef: { project_id: 'demo' }, projectName: 'Demo', localProjectId: 'demo', expectLocalId: 'demo', onUnavailable });

		expect(await rowButton('Home server')).toBeTruthy();
		expect(onUnavailable).not.toHaveBeenCalled();
	});
});

describe('Remove project and chats from a worker', () => {
	const stale = response([worker('self', 'Here', 'ready'), worker('peer', 'Tower', 'ready', { removal_chats: 0, removal_worktree_count: 0, removal_worktrees: [] })]);
	const removeButton = () => screen.getByRole('button', { name: 'Remove' }) as HTMLButtonElement;
	const askRemove = async () => {
		await openRow('Tower');
		await fireEvent.click(screen.getByRole('button', { name: 'Remove project and chats from Tower' }));
	};

	it('waits for a fresh count before it offers Remove, never showing the cached one', async () => {
		let answer!: (value: ReturnType<typeof response>) => void;
		vi.mocked(getProjectWorkers)
			.mockResolvedValueOnce(stale)
			.mockImplementationOnce(() => new Promise((resolve) => { answer = resolve; }));
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await askRemove();
		expect(screen.getByText('Counting the chats this removes…')).toBeTruthy();
		expect(screen.queryByText(/This removes no chats/)).toBeNull();
		expect(removeButton().disabled).toBe(true);

		// A chat was created on Tower since the cached read.
		answer(response([worker('self', 'Here', 'ready'), worker('peer', 'Tower', 'ready', { removal_chats: 1, removal_worktree_count: 0, removal_worktrees: [] })]));
		await screen.findByText(/Remove Demo from Tower and its 1 chat\?/);
		expect(removeButton().disabled).toBe(false);
	});

	it('shows a failed refresh and keeps Remove disabled instead of using the cached count', async () => {
		vi.mocked(getProjectWorkers)
			.mockResolvedValueOnce(stale)
			.mockRejectedValueOnce({ status: 502, detail: 'Tower did not answer' });
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await askRemove();
		expect((await screen.findByRole('alert')).textContent).toContain('Tower did not answer');
		expect(screen.queryByText(/This removes no chats/)).toBeNull();
		expect(removeButton().disabled).toBe(true);
	});

	// Review round 2 S3: a 200 answer is not a count by itself.
	it.each([
		['the worker timed out (200, unknown, no count)', worker('peer', 'Tower', 'unknown', { removal_chats: null }), /Tower didn't answer/],
		['an older worker reports no count', worker('peer', 'Tower', 'ready'), /Tower needs an update before it can remove this project/]
	])('keeps Remove disabled when %s', async (_case, fresh, reason) => {
		vi.mocked(getProjectWorkers)
			.mockResolvedValueOnce(stale)
			.mockResolvedValueOnce(response([worker('self', 'Here', 'ready'), fresh]));
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await askRemove();
		expect((await screen.findByRole('alert')).textContent).toMatch(reason);
		expect(screen.queryByText(/all its chats|This removes no chats/)).toBeNull();
		expect(screen.getByRole('button', { name: 'Remove' }).hasAttribute('disabled')).toBe(true);
	});

	it('offers Remove for a fresh count of zero', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValueOnce(stale).mockResolvedValueOnce(stale);
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await askRemove();
		await screen.findByText(/Remove Demo from Tower\? This removes no chats\./);
		expect(removeButton().disabled).toBe(false);
	});

	it("puts this worker's removal in its own details, behind the same fresh count", async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(stale);
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await openRow('Here');
		const inspector = details('Here');
		expect(within(inspector).getByRole('button', { name: 'Remove project and chats' })).toBeTruthy();
		expect(within(inspector).queryByRole('button', { name: 'Remove project and chats from Here' })).toBeNull();
	});
});
