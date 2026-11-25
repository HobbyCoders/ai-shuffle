/**
 * Add Project is a modal: it names itself, takes focus, closes on Escape, has
 * a named close button, and returns focus to the opener.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
vi.mock('$lib/api/auth', () => ({ getWorkspaceConfig: vi.fn(async () => ({ is_local_mode: false })) }));
vi.mock('$lib/components/DirectoryBrowser.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/git/GitHubRepoPicker.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
const m = vi.hoisted(() => ({
	overview: vi.fn(async () => null as unknown),
	installs: vi.fn(async () => ({ installs: [] as unknown[], self_instance_id: 'self' })),
	cloneNew: vi.fn(),
	setupStatus: vi.fn(),
	startSetup: vi.fn(async () => ({ op_id: 'psetup-peer' })),
	createProject: vi.fn(),
	loadProjects: vi.fn(async () => {})
}));
vi.mock('$lib/api/project-workers', async (orig) => ({
	...(await orig<typeof import('$lib/api/project-workers')>()),
	getProjectsOverview: m.overview,
	cloneNewProject: m.cloneNew,
	getSetupStatus: m.setupStatus,
	startProjectSetup: m.startSetup
}));
vi.mock('$lib/stores/app-data.svelte', async (orig) => {
	const real = await orig<typeof import('$lib/stores/app-data.svelte')>();
	return {
		...real,
		appData: new Proxy(real.appData, {
			get: (target, key) =>
				key === 'createProject' ? m.createProject : key === 'loadProjects' ? m.loadProjects : Reflect.get(target, key)
		})
	};
});
vi.mock('$lib/api/installs', async (orig) => ({
	...(await orig<typeof import('$lib/api/installs')>()),
	listInstalls: m.installs
}));
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import AddProjectModal from './AddProjectModal.svelte';
import { resetWorkerPresence } from '$lib/stores/worker-presence';
import { resetProjectsOverview } from '$lib/stores/projects-overview';

afterEach(() => {
	cleanup();
	resetWorkerPresence();
	resetProjectsOverview();
	document.body.innerHTML = '';
});

describe('AddProjectModal keyboard and focus', () => {
	it('is a named dialog that takes focus, closes on Escape and restores focus', async () => {
		const opener = document.createElement('button');
		document.body.appendChild(opener);
		opener.focus();
		const onClose = vi.fn();
		const view = render(AddProjectModal, { props: { open: true, onClose } });
		await tick();
		await Promise.resolve();

		const dialog = screen.getByRole('dialog', { name: 'New project' });
		expect(dialog.contains(document.activeElement)).toBe(true);

		await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
		expect(onClose).toHaveBeenCalledTimes(1);
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(onClose).toHaveBeenCalledTimes(2);

		await view.rerender({ open: false, onClose });
		await tick();
		expect(document.activeElement).toBe(opener);
	});
});

describe('AddProjectModal placement', () => {
	it('renders its overlay on <body>, not inside the opener, and removes it on close', async () => {
		// The new chat's "in <project>" picker opens it from inside a
		// transformed parent; there the fixed overlay sat under the composer.
		const host = document.createElement('div');
		host.style.transform = 'translateZ(0)';
		document.body.appendChild(host);
		const onClose = vi.fn();
		const view = render(AddProjectModal, { target: host, props: { open: true, onClose } });
		await tick();

		const dialog = screen.getByRole('dialog', { name: 'New project' });
		expect(dialog.parentElement).toBe(document.body);
		expect(host.contains(dialog)).toBe(false);

		await view.rerender({ open: false, onClose });
		await tick();
		expect(screen.queryByRole('dialog', { name: 'New project' })).toBeNull();
	});
});

describe('AddProjectModal "Set it up on"', () => {
	const inst = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
		id, device_name: name, base_url: null, platform: 'linux', version: '1', capabilities: {}, last_seen: null,
		online: true, approved: true, ...extra
	});

	it('lists the other workers once the server can set projects up; Git pre-ticks online ones, plain folders do not', async () => {
		m.overview.mockResolvedValue([]);
		m.installs.mockResolvedValue({
			installs: [inst('self', 'Unraid', { is_self: true }), inst('pc', 'Studio'), inst('lap', 'Laptop', { online: false })],
			self_instance_id: 'self'
		});
		render(AddProjectModal, { props: { open: true, onClose: vi.fn() } });
		await screen.findByText('Set it up on');

		// Folder source: nobody pre-ticked; each worker gets an empty folder.
		const pc = screen.getByRole('checkbox', { name: /Studio/ }) as HTMLInputElement;
		const lap = screen.getByRole('checkbox', { name: /Laptop/ }) as HTMLInputElement;
		expect(pc.checked).toBe(false);
		expect(screen.getAllByText(/files aren't copied/).length).toBeGreaterThan(0);
		expect(screen.getByText(/runs when it's back/)).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Create on 1 worker' })).toBeTruthy();

		// Clone from GitHub: online workers pre-ticked, offline left unticked.
		await fireEvent.click(screen.getByRole('radio', { name: /Clone from GitHub/ }));
		await tick();
		expect((screen.getByRole('checkbox', { name: /Studio/ }) as HTMLInputElement).checked).toBe(true);
		expect((screen.getByRole('checkbox', { name: /Laptop/ }) as HTMLInputElement).checked).toBe(false);
		expect(lap.disabled).toBe(false);
		expect(screen.getByRole('button', { name: 'Create on 2 workers' })).toBeTruthy();
	});

	it('the "this worker" row shows no placeholder dash before a path is chosen', async () => {
		m.overview.mockResolvedValue([]);
		m.installs.mockResolvedValue({ installs: [inst('self', 'Home server', { is_self: true }), inst('pc', 'Studio')], self_instance_id: 'self' });
		render(AddProjectModal, { props: { open: true, onClose: vi.fn() } });
		const group = (await screen.findByText('Set it up on')).parentElement!;
		const selfRow = group.querySelector('.wpick-row.locked')!;
		expect(selfRow.textContent).toContain('this worker');
		expect(selfRow.textContent).not.toMatch(/[—–]/);
	});

	it('says which default applies while the ticks are untouched', async () => {
		m.overview.mockResolvedValue([]);
		m.installs.mockResolvedValue({ installs: [inst('self', 'Home server', { is_self: true }), inst('pc', 'Studio')], self_instance_id: 'self' });
		render(AddProjectModal, { props: { open: true, onClose: vi.fn() } });
		expect(await screen.findByText(/Other workers start unticked for a folder/)).toBeTruthy();
		await fireEvent.click(screen.getByRole('radio', { name: /Clone from GitHub/ }));
		expect(screen.getByText(/Online workers start ticked for a repository/)).toBeTruthy();
		// Once the owner changes a tick, switching the source keeps it.
		await fireEvent.click(screen.getByRole('checkbox', { name: /Studio/ }));
		await fireEvent.click(screen.getByRole('radio', { name: /Folder on/ }));
		await fireEvent.click(screen.getByRole('radio', { name: /Clone from GitHub/ }));
		expect((screen.getByRole('checkbox', { name: /Studio/ }) as HTMLInputElement).checked).toBe(false);
		expect(screen.queryByText(/start ticked for a repository/)).toBeNull();
	});

	it('an older server (no overview) shows no worker list', async () => {
		m.overview.mockResolvedValue(null);
		m.installs.mockResolvedValue({ installs: [inst('self', 'Unraid', { is_self: true }), inst('pc', 'Studio')], self_instance_id: 'self' });
		render(AddProjectModal, { props: { open: true, onClose: vi.fn() } });
		await screen.findByRole('button', { name: 'Add project' });
		expect(screen.queryByText('Set it up on')).toBeNull();
	});
});

describe('AddProjectModal create flow', () => {
	const inst = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
		id, device_name: name, base_url: null, platform: 'linux', version: '1', capabilities: {}, last_seen: null,
		online: true, approved: true, ...extra
	});
	const deferred = <T,>() => {
		let resolve!: (v: T) => void;
		const promise = new Promise<T>((r) => (resolve = r));
		return { promise, resolve };
	};
	const row = (name: string, remote: string, local: string | null) => ({
		project_v2_id: `pv2-${name}`, local_project_id: local, name, kind: 'git', remote, default_branch: 'main',
		session_count: 0, workers: []
	});

	async function openClone(props: Record<string, unknown> = {}) {
		render(AddProjectModal, { props: { open: true, onClose: vi.fn(), ...props } });
		await fireEvent.click(await screen.findByRole('radio', { name: /Clone from GitHub/ }));
	}
	const urlInput = () => screen.getByLabelText('Or paste a clone URL') as HTMLInputElement;

	afterEach(() => {
		vi.clearAllMocks();
	});

	it('sends the request as it was when submitted, locks the form, and starts peers with that snapshot', async () => {
		m.overview.mockResolvedValue([]);
		m.installs.mockResolvedValue({ installs: [inst('self', 'Home server', { is_self: true }), inst('pc', 'Studio')], self_instance_id: 'self' });
		m.cloneNew.mockResolvedValue({ op_id: 'psetup-1', project_v2_id: 'pv2', project_id: 'repo-a', status: 'queued' });
		const status = deferred<unknown>();
		m.setupStatus.mockReturnValue(status.promise);
		const onCreated = vi.fn();
		const onClose = vi.fn();
		await openClone({ onCreated, onClose });
		await screen.findByRole('checkbox', { name: /Studio/ });
		await fireEvent.input(urlInput(), { target: { value: 'https://github.com/acme/repo-a.git' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Create on 2 workers' }));

		expect(m.cloneNew).toHaveBeenCalledWith({ id: 'repo-a', name: 'repo-a', clone_url: 'https://github.com/acme/repo-a.git', folder: 'repo-a' });
		const fieldset = urlInput().closest('fieldset') as HTMLFieldSetElement;
		expect(fieldset.disabled).toBe(true);
		// Escape does not drop a running create; the dialog says the clone keeps running if closed.
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(onClose).not.toHaveBeenCalled();
		expect(screen.getByRole('status').textContent).toMatch(/keeps the clone running/);

		// An edit that slips in while the clone runs changes nothing that was sent.
		await fireEvent.input(urlInput(), { target: { value: 'https://github.com/acme/other.git' } });
		status.resolve({ op_id: 'psetup-1', status: 'done', progress: 100, project_id: 'repo-a', project_v2_id: 'pv2' });
		await vi.waitFor(() => expect(onCreated).toHaveBeenCalledWith('repo-a'));
		expect(m.startSetup).toHaveBeenCalledWith({ project_id: 'repo-a' }, { install_id: 'pc', folder: 'repo-a' });
		expect(m.cloneNew).toHaveBeenCalledTimes(1);
		expect(onClose).toHaveBeenCalledTimes(1);
	});

	it('a failed clone keeps the dialog open with the reason, selects nothing, and Retry sends it again', async () => {
		m.overview.mockResolvedValue([]);
		m.installs.mockResolvedValue({ installs: [inst('self', 'Home server', { is_self: true })], self_instance_id: 'self' });
		m.cloneNew.mockResolvedValue({ op_id: 'psetup-1', project_v2_id: 'pv2', project_id: 'missing', status: 'queued' });
		m.setupStatus.mockResolvedValue({ op_id: 'psetup-1', status: 'error', progress: null, error_code: 'not_found',
			error: "The repository wasn't found, or this worker's GitHub account can't see it.", project_id: null, project_v2_id: 'pv2' });
		const onCreated = vi.fn();
		const onClose = vi.fn();
		await openClone({ onCreated, onClose });
		await fireEvent.input(urlInput(), { target: { value: 'https://github.com/acme/missing.git' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Clone project' }));

		expect(await screen.findByText(/repository wasn't found/)).toBeTruthy();
		expect(onCreated).not.toHaveBeenCalled();
		expect(onClose).not.toHaveBeenCalled();
		expect(m.startSetup).not.toHaveBeenCalled();
		expect((urlInput().closest('fieldset') as HTMLFieldSetElement).disabled).toBe(false);

		m.setupStatus.mockResolvedValue({ op_id: 'psetup-2', status: 'done', progress: 100, project_id: 'missing', project_v2_id: 'pv2' });
		await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
		await vi.waitFor(() => expect(onCreated).toHaveBeenCalledWith('missing'));
		expect(m.cloneNew).toHaveBeenCalledTimes(2);
	});

	it('a clone the server settled as cancelled reads as cancelled, never as a failure', async () => {
		const { notify } = await import('$lib/stores/notifications');
		const error = vi.spyOn(notify, 'error').mockReturnValue('n1');
		m.overview.mockResolvedValue([]);
		m.installs.mockResolvedValue({ installs: [inst('self', 'Home server', { is_self: true })], self_instance_id: 'self' });
		m.cloneNew.mockResolvedValue({ op_id: 'psetup-1', project_v2_id: 'pv2', project_id: 'repo-c', status: 'queued' });
		// Cancelled elsewhere (say, from the Workers tab): an error row with code 'cancelled'.
		m.setupStatus.mockResolvedValue({ op_id: 'psetup-1', status: 'error', progress: null, error_code: 'cancelled',
			error: 'Setup was cancelled.', project_id: null, project_v2_id: 'pv2' });
		await openClone();
		await fireEvent.input(urlInput(), { target: { value: 'https://github.com/acme/repo-c.git' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Clone project' }));

		expect(await screen.findByText('Clone cancelled. Nothing was added.')).toBeTruthy();
		expect(screen.queryByText('Setup was cancelled.')).toBeNull();
		expect(error).not.toHaveBeenCalled();
		error.mockRestore();
	});

	it('drops the last failure once another repository is entered', async () => {
		m.overview.mockResolvedValue([]);
		m.installs.mockResolvedValue({ installs: [inst('self', 'Home server', { is_self: true })], self_instance_id: 'self' });
		m.cloneNew.mockResolvedValue({ op_id: 'psetup-1', project_v2_id: 'pv2', project_id: 'missing', status: 'queued' });
		m.setupStatus.mockResolvedValue({ op_id: 'psetup-1', status: 'error', progress: null, error_code: 'not_found',
			error: "The repository wasn't found.", project_id: null, project_v2_id: 'pv2' });
		await openClone();
		await fireEvent.input(urlInput(), { target: { value: 'https://github.com/acme/missing.git' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Clone project' }));
		expect(await screen.findByText(/repository wasn't found/)).toBeTruthy();
		await fireEvent.input(urlInput(), { target: { value: 'https://github.com/acme/missing.git ' } });
		expect(screen.getByText(/repository wasn't found/)).toBeTruthy();
		await fireEvent.input(urlInput(), { target: { value: 'https://github.com/acme/other.git' } });
		expect(screen.queryByText(/repository wasn't found/)).toBeNull();
		expect(screen.getByRole('button', { name: 'Clone project' })).toBeTruthy();
	});

	it('says which worker is already cloning the repository, with no Retry', async () => {
		m.overview.mockResolvedValue([]);
		m.installs.mockResolvedValue({ installs: [inst('self', 'Home server', { is_self: true })], self_instance_id: 'self' });
		const message = 'Studio is cloning this repository; wait for it or cancel it there.';
		m.cloneNew.mockRejectedValueOnce({ detail: message, status: 409,
			body: { detail: { error_code: 'being_set_up', worker: 'Studio', error: message } } });
		const onCreated = vi.fn();
		await openClone({ onCreated });
		await fireEvent.input(urlInput(), { target: { value: 'https://github.com/acme/busy.git' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Clone project' }));

		expect(await screen.findByText(message)).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
		expect(onCreated).not.toHaveBeenCalled();
		expect(m.setupStatus).not.toHaveBeenCalled();
	});

	it('names a worker that refused to start its setup', async () => {
		const { notify } = await import('$lib/stores/notifications');
		const warning = vi.spyOn(notify, 'warning').mockReturnValue('n1');
		m.overview.mockResolvedValue([]);
		m.installs.mockResolvedValue({ installs: [inst('self', 'Home server', { is_self: true }), inst('pc', 'Studio')], self_instance_id: 'self' });
		m.cloneNew.mockResolvedValue({ op_id: 'psetup-1', project_v2_id: 'pv2', project_id: 'repo-a', status: 'queued' });
		m.setupStatus.mockResolvedValue({ op_id: 'psetup-1', status: 'done', progress: 100, project_id: 'repo-a', project_v2_id: 'pv2' });
		m.startSetup.mockRejectedValueOnce({ detail: 'That worker is offline', status: 409 });
		const onCreated = vi.fn();
		await openClone({ onCreated });
		await screen.findByRole('checkbox', { name: /Studio/ });
		await fireEvent.input(urlInput(), { target: { value: 'https://github.com/acme/repo-a.git' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Create on 2 workers' }));
		await vi.waitFor(() => expect(onCreated).toHaveBeenCalledWith('repo-a'));
		expect(warning).toHaveBeenCalledWith(expect.stringContaining('Studio: That worker is offline'));
		warning.mockRestore();
	});

	it('marks a pasted repository that is already a project and offers to open it', async () => {
		m.overview.mockResolvedValue([row('Site', 'github.com/acme/site', 'site')]);
		m.installs.mockResolvedValue({ installs: [inst('self', 'Home server', { is_self: true })], self_instance_id: 'self' });
		const onCreated = vi.fn();
		await openClone({ onCreated });
		await fireEvent.input(urlInput(), { target: { value: 'git@github.com:Acme/site.git' } });
		const note = await screen.findByTestId('already-added');
		expect(note.textContent).toContain('Already added as Site');
		expect((screen.getByRole('button', { name: 'Clone project' }) as HTMLButtonElement).disabled).toBe(true);
		await fireEvent.click(screen.getByRole('button', { name: 'Open it' }));
		expect(onCreated).toHaveBeenCalledWith('site');
		expect(m.cloneNew).not.toHaveBeenCalled();
	});

	it('keeps the Project ID under Advanced and checks the server rules inline', async () => {
		m.overview.mockResolvedValue([]);
		m.installs.mockResolvedValue({ installs: [inst('self', 'Home server', { is_self: true })], self_instance_id: 'self' });
		await openClone();
		await fireEvent.input(urlInput(), { target: { value: 'https://github.com/acme/My_Repo.git' } });
		const details = screen.getByText('Advanced').closest('details') as HTMLDetailsElement;
		expect(details.open).toBe(false);
		const id = screen.getByLabelText('Project ID') as HTMLInputElement;
		expect(details.contains(id)).toBe(true);
		expect(id.value).toBe('my-repo');
		expect(screen.getByTestId('clone-destination').textContent).toContain('my-repo');
		expect(screen.getByTestId('clone-destination').textContent).toContain('Home server');

		await fireEvent.input(id, { target: { value: 'Bad ID' } });
		expect(screen.getByText(/lowercase letters, numbers and dashes only/i)).toBeTruthy();
		expect(details.open).toBe(true);
		expect((screen.getByRole('button', { name: 'Clone project' }) as HTMLButtonElement).disabled).toBe(true);
		await fireEvent.input(id, { target: { value: 'a'.repeat(51) } });
		expect(screen.getByText('Use 50 characters or fewer.')).toBeTruthy();
		await fireEvent.input(id, { target: { value: 'ok-id' } });
		await fireEvent.input(screen.getByLabelText('Display name'), { target: { value: 'x'.repeat(101) } });
		expect(screen.getByText('Use 100 characters or fewer.')).toBeTruthy();
		expect((screen.getByRole('button', { name: 'Clone project' }) as HTMLButtonElement).disabled).toBe(true);
	});
});

describe('remoteKey', () => {
	it('matches the same repository in any spelling', async () => {
		const { remoteKey } = await import('$lib/api/project-workers');
		const want = 'github.com/acme/site';
		for (const url of ['https://github.com/Acme/site.git', 'https://token@github.com/acme/site/', 'git@github.com:acme/site.git',
			'ssh://git@github.com/acme/site', 'github.com/acme/site']) {
			expect(remoteKey(url)).toBe(want);
		}
		expect(remoteKey('')).toBe('');
	});
});
