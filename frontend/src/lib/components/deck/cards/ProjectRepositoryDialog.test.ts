/** "Change repository on all workers": one switch for every worker's copy, and what happened on each. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
vi.mock('$lib/stores/app-data.svelte', () => ({ appData: { loadProjectCatalog: vi.fn() } }));
vi.mock('$lib/api/project-workers', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/project-workers')>()),
	changeProjectRepository: vi.fn()
}));
import ProjectRepositoryDialog from './ProjectRepositoryDialog.svelte';
import { changeProjectRepository } from '$lib/api/project-workers';
import { appData } from '$lib/stores/app-data.svelte';

const ref = { project_v2_id: 'pv2-demo' };
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('Change repository on all workers', () => {
	it('switches every worker at once and says what happened on each', async () => {
		vi.mocked(changeProjectRepository).mockResolvedValue({
			project_v2_id: 'pv2-demo', name: 'Demo', repository: 'github.com/someone/demo', remote_url: 'https://github.com/someone/demo.git',
			previous_repository: 'github.com/example/demo', kept_as_upstream: true, default_branch: 'main', merged_duplicate: false,
			workers: [{ install_id: 'self', device_name: 'Here', is_self: true, status: 'updated', ok: true },
				{ install_id: 'peer', device_name: 'Tower', is_self: false, status: 'offline', ok: true }],
			workers_without_copy: []
		});
		const onChanged = vi.fn();
		const onClose = vi.fn();
		render(ProjectRepositoryDialog, { projectRef: ref, remote: 'github.com/example/demo', onClose, onChanged });

		expect(screen.getByRole('dialog', { name: 'Change repository on all workers' })).toBeTruthy();
		const submit = screen.getByRole('button', { name: 'Switch repository' }) as HTMLButtonElement;
		expect(submit.disabled).toBe(true);
		await fireEvent.input(screen.getByLabelText('New repository URL'), { target: { value: ' https://github.com/someone/demo.git ' } });
		await fireEvent.click(screen.getByRole('switch', { name: 'Keep the current one as upstream' }));
		await fireEvent.click(submit);

		expect(changeProjectRepository).toHaveBeenCalledWith(ref, 'https://github.com/someone/demo.git', true);
		const result = await screen.findByText(/Now using github.com\/someone\/demo\./);
		expect(result.textContent).toContain('github.com/example/demo stays available as the upstream remote.');
		expect(result.textContent).toContain('Switched on Here.');
		expect(result.textContent).toContain('Tower switches when it comes back online.');
		expect(screen.queryByLabelText('New repository URL')).toBeNull();
		expect(appData.loadProjectCatalog).toHaveBeenCalled();
		expect(onChanged).toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Done' }));
		expect(onClose).toHaveBeenCalled();
	});

	it('keeps the form open with the reason when the server refuses', async () => {
		vi.mocked(changeProjectRepository).mockRejectedValue({ status: 400, detail: 'The project “Other” already uses that repository.' });
		render(ProjectRepositoryDialog, { projectRef: ref, remote: 'github.com/example/demo', onClose: vi.fn() });

		await fireEvent.input(screen.getByLabelText('New repository URL'), { target: { value: 'https://github.com/example/other.git' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Switch repository' }));

		expect((await screen.findByRole('alert')).textContent).toContain('already uses that repository');
		expect(screen.getByLabelText('New repository URL')).toBeTruthy();
	});

	it('offers no upstream switch when the project has no repository yet', async () => {
		render(ProjectRepositoryDialog, { projectRef: ref, remote: null, onClose: vi.fn() });
		expect(screen.queryByRole('switch')).toBeNull();
		expect(screen.getByText('None')).toBeTruthy();
	});
});
