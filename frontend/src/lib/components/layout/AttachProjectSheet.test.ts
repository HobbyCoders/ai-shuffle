/**
 * #371 step 4: the attach sheet gives a running no-project session its
 * project. It must state the one-way warning before the confirm, refuse while
 * blocked, and mirror the converted row once the backend accepts.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({
	attachProject: vi.fn(),
	applyAttachedProject: vi.fn(),
	notify: vi.fn(),
}));

vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable: w } = await import('svelte/store');
	return {
		projects: w([
			{ id: 'proj-local', name: 'ai-shuffle', path: '/w/ai-shuffle' },
			{ id: 'proj-two', name: 'site', path: '/w/site' },
		]),
		clusterProjects: w([
			{ project_v2_id: 'v2-remote', origin: 'git@example.com:me/remote-only.git', name: 'remote-only', local_project_id: null, eligible: [{ id: 'peer', online: true }] },
		]),
	};
});
vi.mock('$lib/api/sessions', () => ({ attachProject: mocks.attachProject }));
vi.mock('$lib/stores/active-session', () => ({ activeSession: { applyAttachedProject: mocks.applyAttachedProject } }));
vi.mock('$lib/stores/notifications', () => ({ notify: mocks.notify }));

import AttachProjectSheet from './AttachProjectSheet.svelte';

const props = (over: Partial<{ blockedReason: string | null; onClose: () => void }> = {}) => ({
	open: true,
	sessionId: 'sess-1',
	agentName: 'Juniper',
	blockedReason: null,
	onClose: vi.fn(),
	...over,
});

beforeEach(() => {
	mocks.attachProject.mockReset();
	mocks.applyAttachedProject.mockReset();
	mocks.notify.mockReset();
});
afterEach(() => cleanup());

describe('AttachProjectSheet', () => {
	it('shows the one-way warning before anything is picked, and names the project once one is', async () => {
		render(AttachProjectSheet, { props: props() });

		const note = screen.getByRole('note');
		expect(note.textContent).toContain('One-way. Projects can’t be detached.');
		expect(note.textContent).toContain('Juniper keeps this conversation');

		await fireEvent.click(screen.getByRole('radio', { name: /ai-shuffle/ }));
		expect(screen.getByRole('note').textContent).toContain('ai-shuffle’s checkout');
	});

	it('keeps the confirm disabled until a local project is picked', async () => {
		render(AttachProjectSheet, { props: props() });
		const confirm = () => screen.getByRole('button', { name: /^attach/i }) as HTMLButtonElement;

		expect(confirm().disabled).toBe(true);
		// A project only another worker has cannot be attached from here.
		const remote = screen.getByRole('radio', { name: /remote-only/ });
		expect(remote.getAttribute('aria-disabled')).toBe('true');
		await fireEvent.click(remote);
		expect(confirm().disabled).toBe(true);

		await fireEvent.click(screen.getByRole('radio', { name: /site/ }));
		expect(confirm().disabled).toBe(false);
	});

	it('refuses while blocked and says why', async () => {
		render(AttachProjectSheet, { props: props({ blockedReason: 'Wait for this turn to finish, then attach.' }) });
		await fireEvent.click(screen.getByRole('radio', { name: /ai-shuffle/ }));

		expect(screen.getByText('Wait for this turn to finish, then attach.')).toBeTruthy();
		expect((screen.getByRole('button', { name: /^attach/i }) as HTMLButtonElement).disabled).toBe(true);
	});

	it('attaches through the API, mirrors the converted session and closes', async () => {
		mocks.attachProject.mockResolvedValue({ session_id: 'sess-1', project_id: 'proj-local', project_v2_id: 'v2-local', mode: 'code' });
		const onClose = vi.fn();
		render(AttachProjectSheet, { props: props({ onClose }) });

		await fireEvent.click(screen.getByRole('radio', { name: /ai-shuffle/ }));
		await fireEvent.click(screen.getByRole('button', { name: /^attach/i }));

		await waitFor(() => expect(onClose).toHaveBeenCalled());
		expect(mocks.attachProject).toHaveBeenCalledWith('sess-1', 'proj-local');
		expect(mocks.applyAttachedProject).toHaveBeenCalledWith('sess-1', 'proj-local', 'v2-local');
		expect(mocks.notify).toHaveBeenCalled();
	});

	it('keeps the sheet open with the server’s reason when the attach is refused', async () => {
		mocks.attachProject.mockRejectedValue(new Error('This session already has a project.'));
		const onClose = vi.fn();
		render(AttachProjectSheet, { props: props({ onClose }) });

		await fireEvent.click(screen.getByRole('radio', { name: /ai-shuffle/ }));
		await fireEvent.click(screen.getByRole('button', { name: /^attach/i }));

		await waitFor(() => expect(screen.getByText(/already has a project/)).toBeTruthy());
		expect(onClose).not.toHaveBeenCalled();
		expect(mocks.applyAttachedProject).not.toHaveBeenCalled();
	});
});
