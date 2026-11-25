/**
 * Workers tab: change a project's repository on every worker at once, and
 * take a project no worker holds off the list.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));
vi.mock('$lib/stores/app-data.svelte', () => ({ appData: { loadProjectCatalog: vi.fn() } }));
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
	changeProjectRepository: vi.fn(),
	forgetProject: vi.fn()
}));
import ProjectWorkersTab from './ProjectWorkersTab.svelte';
import { changeProjectRepository, forgetProject, getProjectWorkers } from '$lib/api/project-workers';
import { appData } from '$lib/stores/app-data.svelte';

const worker = (install_id: string, device_name: string, state: string, extra: Record<string, unknown> = {}) => ({
	install_id, device_name, is_self: install_id === 'self', online: true, approved: true, state, setup: null,
	folder: state === 'ready' ? `/work/${install_id}/demo` : null, suggested_folder: 'demo', branch: state === 'ready' ? 'main' : null,
	dirty: false, changed_files: 0, last_seen: null, ...extra
});
const response = (workers: ReturnType<typeof worker>[], remote = 'github.com/example/demo') => ({
	project_v2_id: 'pv2-demo', name: 'Demo', kind: 'git' as const, remote, default_folder: 'demo', workers
});
const ref = { project_v2_id: 'pv2-demo' };

beforeEach(() => {
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

describe('Change repository', () => {
	it('switches every worker at once and says what happened on each', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(response([worker('self', 'Here', 'ready'), worker('peer', 'Tower', 'offline_has', { online: false })]));
		vi.mocked(changeProjectRepository).mockResolvedValue({
			project_v2_id: 'pv2-demo', name: 'Demo', repository: 'github.com/someone/demo', remote_url: 'https://github.com/someone/demo.git',
			previous_repository: 'github.com/example/demo', kept_as_upstream: true, default_branch: 'main', merged_duplicate: false,
			workers: [{ install_id: 'self', device_name: 'Here', is_self: true, status: 'updated', ok: true },
				{ install_id: 'peer', device_name: 'Tower', is_self: false, status: 'offline', ok: true }],
			workers_without_copy: []
		});
		const onChanged = vi.fn();
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo', onChanged });

		await fireEvent.click(await screen.findByRole('button', { name: 'Change repository' }));
		const submit = screen.getByRole('button', { name: 'Switch repository' }) as HTMLButtonElement;
		expect(submit.disabled).toBe(true);
		await fireEvent.input(screen.getByLabelText('New repository URL'), { target: { value: ' https://github.com/someone/demo.git ' } });
		await fireEvent.click(screen.getByRole('switch'));
		await fireEvent.click(submit);

		expect(changeProjectRepository).toHaveBeenCalledWith(ref, 'https://github.com/someone/demo.git', true);
		const banner = await screen.findByText(/Now using github.com\/someone\/demo\./);
		expect(banner.textContent).toContain('github.com/example/demo stays available as the upstream remote.');
		expect(banner.textContent).toContain('Switched on Here.');
		expect(banner.textContent).toContain('Tower switches when it comes back online.');
		expect(screen.queryByLabelText('New repository URL')).toBeNull();
		expect(appData.loadProjectCatalog).toHaveBeenCalled();
		expect(onChanged).toHaveBeenCalled();
	});

	it('keeps the form open with the reason when the server refuses', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(response([worker('self', 'Here', 'ready')]));
		vi.mocked(changeProjectRepository).mockRejectedValue({ status: 400, detail: 'The project “Other” already uses that repository.' });
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await fireEvent.click(await screen.findByRole('button', { name: 'Change repository' }));
		await fireEvent.input(screen.getByLabelText('New repository URL'), { target: { value: 'https://github.com/example/other.git' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Switch repository' }));

		expect((await screen.findByRole('alert')).textContent).toContain('already uses that repository');
		expect(screen.getByLabelText('New repository URL')).toBeTruthy();
	});

	it('is not offered for a plain-folder project', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue({ ...response([worker('self', 'Here', 'ready')], ''), kind: 'folder' });
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });
		await screen.findByText('Ready');
		expect(screen.queryByRole('button', { name: 'Change repository' })).toBeNull();
	});
});

describe('Remove project and chats from a worker', () => {
	const stale = response([worker('self', 'Here', 'ready'), worker('peer', 'Tower', 'ready', { removal_chats: 0, removal_worktree_count: 0, removal_worktrees: [] })]);
	const removeButton = () => screen.getByRole('button', { name: 'Remove' }) as HTMLButtonElement;

	it('waits for a fresh count before it offers Remove, never showing the cached one', async () => {
		let answer!: (value: ReturnType<typeof response>) => void;
		vi.mocked(getProjectWorkers)
			.mockResolvedValueOnce(stale)
			.mockImplementationOnce(() => new Promise((resolve) => { answer = resolve; }));
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await fireEvent.click(await screen.findByRole('button', { name: 'Remove project and chats from Tower' }));
		expect(screen.getByText('Counting the chats this removes…')).toBeTruthy();
		expect(screen.queryByText(/It has no chats/)).toBeNull();
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

		await fireEvent.click(await screen.findByRole('button', { name: 'Remove project and chats from Tower' }));
		expect((await screen.findByRole('alert')).textContent).toContain('Tower did not answer');
		expect(screen.queryByText(/It has no chats/)).toBeNull();
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

		await fireEvent.click(await screen.findByRole('button', { name: 'Remove project and chats from Tower' }));
		expect((await screen.findByRole('alert')).textContent).toMatch(reason);
		expect(screen.queryByText(/all its chats|It has no chats/)).toBeNull();
		expect(removeButton().disabled).toBe(true);
	});

	it('offers Remove for a fresh count of zero', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValueOnce(stale).mockResolvedValueOnce(stale);
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: 'demo' });

		await fireEvent.click(await screen.findByRole('button', { name: 'Remove project and chats from Tower' }));
		await screen.findByText(/Remove Demo from Tower\? It has no chats\./);
		expect(removeButton().disabled).toBe(false);
	});
});

describe('Remove from list', () => {
	it('offers it only when no worker holds a copy, then hands the project back', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(response([
			worker('self', 'Here', 'failed', { setup: { op_id: 'op', status: 'error', progress: null, error: 'Folder exists.' } }),
			worker('peer', 'Tower', 'missing')
		]));
		vi.mocked(forgetProject).mockResolvedValue({ ok: true, project_v2_id: 'pv2-demo' });
		const onRemoved = vi.fn();
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: null, onRemoved });

		await fireEvent.click(await screen.findByRole('button', { name: 'Remove from list' }));
		expect(forgetProject).toHaveBeenCalledWith(ref);
		await waitFor(() => expect(onRemoved).toHaveBeenCalledOnce());
	});

	it('is hidden while any worker still has it', async () => {
		vi.mocked(getProjectWorkers).mockResolvedValue(response([worker('self', 'Here', 'missing'), worker('peer', 'Tower', 'offline_has', { online: false })]));
		render(ProjectWorkersTab, { projectRef: ref, projectName: 'Demo', localProjectId: null });
		await screen.findByRole('button', { name: 'Change repository' });
		expect(screen.queryByRole('button', { name: 'Remove from list' })).toBeNull();
	});
});
