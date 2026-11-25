/**
 * Worker table: one compact row per worker with its status, folder and
 * branch; selecting a row opens that worker's inspector (beside the table,
 * or as a sheet on a phone), where its folder is changed and its copy
 * removed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
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
	getProjectRemovalPreview: vi.fn()
}));
import ProjectWorkersTab from './ProjectWorkersTab.svelte';
import { getProjectWorkers, startProjectSetup } from '$lib/api/project-workers';

const worker = (install_id: string, device_name: string, state: string, extra: Record<string, unknown> = {}) => ({
	install_id, device_name, is_self: install_id === 'self', online: true, approved: true, state, setup: null,
	folder: state === 'ready' ? `/workspace/example/${install_id}/demo` : null, suggested_folder: 'demo', branch: state === 'ready' ? 'main' : null,
	dirty: false, changed_files: 0, last_seen: null, folder_revision: state === 'ready' ? 'rev-1' : null, ...extra
});
const response = (workers: ReturnType<typeof worker>[], remote = 'github.com/example/demo') => ({
	project_v2_id: 'pv2-demo', name: 'Demo', kind: 'git' as const, remote, default_folder: 'demo', workers
});
const ref = { project_v2_id: 'pv2-demo' };
const openRow = async (name: string) => fireEvent.click(await screen.findByRole('button', { name: `${name}: open its details` }));

beforeEach(() => {
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

describe('Worker table', () => {
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

		const table = await screen.findByRole('table', { name: 'Workers' });
		const rows = within(table).getAllByRole('row').slice(1);
		expect(rows.map((r) => r.getAttribute('data-worker-state'))).toEqual(['ready', 'setting_up', 'failed', 'queued_offline', 'missing']);
		expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Worker', 'Status', 'Folder', 'Branch', 'Last seen']);

		expect(rows[0].textContent).toContain('This worker · Online');
		expect(rows[0].textContent).toContain('Ready');
		expect(rows[0].querySelector('.pw-cell-folder')?.getAttribute('title')).toBe('/workspace/example/self/demo');
		expect(rows[0].textContent).toContain('main');
		expect(rows[1].textContent).toContain('Cloning 40%');
		expect(rows[2].textContent).toContain('Failed');
		expect(within(rows[2]).getByRole('button', { name: 'Retry' })).toBeTruthy();
		expect(rows[3].textContent).toContain('Queued until online');
		expect(rows[4].textContent).toContain('Not set up');
		expect(within(rows[4]).getByRole('button', { name: 'Set up' })).toBeTruthy();

		// One folder editor per worker, in its inspector; never a raw path box.
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

	it('opens the selected worker beside the table and drops the Branch and Last seen columns', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(mixed);
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await openRow('Home server');
		const inspector = screen.getByRole('complementary', { name: 'Home server details' });
		expect(within(inspector).getByRole('button', { name: 'New chat here' })).toBeTruthy();
		expect(within(inspector).getByRole('button', { name: 'Change folder' })).toBeTruthy();
		expect(within(screen.getByRole('table')).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Worker', 'Status', 'Folder']);
		expect(screen.getByRole('button', { name: 'Home server: open its details' }).getAttribute('aria-pressed')).toBe('true');

		await fireEvent.click(within(inspector).getByRole('button', { name: 'Change folder' }));
		expect(within(inspector).getByRole('textbox', { name: 'New folder on Home server' })).toBeTruthy();
		expect(within(inspector).getByRole('button', { name: 'Browse' })).toBeTruthy();

		await fireEvent.click(screen.getByRole('button', { name: 'Close Home server details' }));
		expect(screen.queryByRole('complementary')).toBeNull();
		expect(screen.getAllByRole('columnheader')).toHaveLength(5);
	});

	it('explains a failure, a running setup and a queued one in the inspector', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(mixed);
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await openRow('Desktop');
		let inspector = screen.getByRole('complementary', { name: 'Desktop details' });
		expect(within(inspector).getByRole('progressbar', { name: 'Setting up on Desktop' })).toBeTruthy();
		expect(inspector.textContent).toContain('You can leave this page');
		expect(within(inspector).getByRole('button', { name: 'Cancel setup' })).toBeTruthy();

		await openRow('Laptop');
		inspector = screen.getByRole('complementary', { name: 'Laptop details' });
		expect(inspector.textContent).toContain('Folder exists.');
		expect(within(inspector).getByRole('button', { name: 'Retry' })).toBeTruthy();

		await openRow('Storage box');
		inspector = screen.getByRole('complementary', { name: 'Storage box details' });
		expect(inspector.textContent).toContain('It sets up when Storage box comes back online.');
		expect(within(inspector).getByRole('button', { name: 'Withdraw' })).toBeTruthy();
	});

	it('sets a missing worker up from its row', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(mixed);
		vi.mocked(startProjectSetup).mockResolvedValue({ op_id: 'op-9', status: 'running' } as never);
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		const row = (await screen.findAllByRole('row')).find((r) => r.getAttribute('data-worker-state') === 'missing')!;
		await fireEvent.click(within(row).getByRole('button', { name: 'Set up' }));
		// The default folder, as suggested; the worker opens so its progress shows.
		expect(startProjectSetup).toHaveBeenCalledWith(ref, { install_id: 'spare', folder: 'demo', mode: 'auto' });
		expect(screen.getByRole('complementary', { name: 'Spare details' })).toBeTruthy();
	});

	it('lists workers as rows on a phone and opens the inspector as a sheet', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(mixed);
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo', stacked: true });

		const list = await screen.findByRole('list', { name: 'Workers' });
		expect(screen.queryByRole('table')).toBeNull();
		expect(within(list).getAllByRole('listitem')).toHaveLength(5);
		await fireEvent.click(within(list).getByText('Laptop').closest('button')!);
		const sheet = await screen.findByRole('dialog', { name: 'Laptop' });
		expect(within(sheet).getByRole('button', { name: 'Retry' })).toBeTruthy();
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

	it("puts this worker's removal in its own inspector, behind the same fresh count", async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(stale);
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await openRow('Here');
		const inspector = screen.getByRole('complementary', { name: 'Here details' });
		expect(within(inspector).getByRole('button', { name: 'Remove project and chats' })).toBeTruthy();
		expect(within(inspector).queryByRole('button', { name: 'Remove project and chats from Here' })).toBeNull();
	});
});
