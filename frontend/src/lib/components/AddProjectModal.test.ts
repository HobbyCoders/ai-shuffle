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
	installs: vi.fn(async () => ({ installs: [] as unknown[], self_instance_id: 'self' }))
}));
vi.mock('$lib/api/project-workers', async (orig) => ({
	...(await orig<typeof import('$lib/api/project-workers')>()),
	getProjectsOverview: m.overview
}));
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

describe('AddProjectModal "Set it up on"', () => {
	const inst = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
		id, device_name: name, base_url: null, platform: 'linux', version: '1', capabilities: {}, last_seen: null,
		online: true, approved: true, ...extra
	});

	it('lists the other workers once the server can set projects up; Git pre-ticks online ones, plain folders do not', async () => {
		m.overview.mockResolvedValue([]);
		m.installs.mockResolvedValue({
			installs: [inst('self', 'Unraid', { is_self: true }), inst('pc', 'GamingPC'), inst('lap', 'Laptop', { online: false })],
			self_instance_id: 'self'
		});
		render(AddProjectModal, { props: { open: true, onClose: vi.fn() } });
		await screen.findByText('Set it up on');

		// Folder source: nobody pre-ticked; each worker gets an empty folder.
		const pc = screen.getByRole('checkbox', { name: /GamingPC/ }) as HTMLInputElement;
		const lap = screen.getByRole('checkbox', { name: /Laptop/ }) as HTMLInputElement;
		expect(pc.checked).toBe(false);
		expect(screen.getAllByText(/files aren't copied/).length).toBeGreaterThan(0);
		expect(screen.getByText(/runs when it's back/)).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Create on 1 worker' })).toBeTruthy();

		// Clone from GitHub: online workers pre-ticked, offline left unticked.
		await fireEvent.click(screen.getByRole('radio', { name: /Clone from GitHub/ }));
		await tick();
		expect((screen.getByRole('checkbox', { name: /GamingPC/ }) as HTMLInputElement).checked).toBe(true);
		expect((screen.getByRole('checkbox', { name: /Laptop/ }) as HTMLInputElement).checked).toBe(false);
		expect(lap.disabled).toBe(false);
		expect(screen.getByRole('button', { name: 'Create on 2 workers' })).toBeTruthy();
	});

	it('an older server (no overview) shows no worker list', async () => {
		m.overview.mockResolvedValue(null);
		m.installs.mockResolvedValue({ installs: [inst('self', 'Unraid', { is_self: true }), inst('pc', 'GamingPC')], self_instance_id: 'self' });
		render(AddProjectModal, { props: { open: true, onClose: vi.fn() } });
		await screen.findByRole('button', { name: 'Add Project' });
		expect(screen.queryByText('Set it up on')).toBeNull();
	});
});
