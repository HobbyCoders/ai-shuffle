import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { DesktopUpdateState } from '$lib/services/desktop';

const shell = vi.hoisted(() => ({
	getState: vi.fn(), check: vi.fn(), install: vi.fn(), unsubscribe: vi.fn(),
	listener: null as ((state: DesktopUpdateState) => void) | null,
	available: true
}));
vi.mock('$lib/services/desktop', () => ({
	get desktop() {
		return shell.available ? {
			version: '1.2.1',
			updates: {
				getState: shell.getState, check: shell.check, install: shell.install,
				onState: (listener: (state: DesktopUpdateState) => void) => {
					shell.listener = listener;
					return shell.unsubscribe;
				}
			}
		} : null;
	}
}));
import DesktopUpdatesSection from './DesktopUpdatesSection.svelte';

const idle: DesktopUpdateState = { phase: 'idle', version: null, percent: null, error: null, checkedAt: null };
beforeEach(() => {
	vi.resetAllMocks();
	shell.available = true;
	shell.listener = null;
	shell.getState.mockResolvedValue(idle);
});
afterEach(cleanup);

it('checks from the UI, follows download progress, and restarts only on request', async () => {
	const view = render(DesktopUpdatesSection);
	expect(screen.getByText('Installed version 1.2.1')).toBeTruthy();
	const button = screen.getByRole('button', { name: 'Check for updates' });
	await waitFor(() => expect(button.hasAttribute('disabled')).toBe(false));
	shell.check.mockImplementation(async () => {
		shell.listener?.({ ...idle, phase: 'downloading', version: '1.2.2', percent: 38 });
	});
	await fireEvent.click(button);
	expect(shell.check).toHaveBeenCalledOnce();
	await screen.findByText('Downloading version 1.2.2 — 38%');
	expect(button.hasAttribute('disabled')).toBe(true);
	shell.listener?.({ ...idle, phase: 'ready', version: '1.2.2', percent: 100 });
	const restart = await screen.findByRole('button', { name: 'Restart and update' });
	expect(shell.install).not.toHaveBeenCalled();
	await fireEvent.click(restart);
	expect(shell.install).toHaveBeenCalledOnce();
	view.unmount();
	expect(shell.unsubscribe).toHaveBeenCalledOnce();
});

it('shows no-update results and update errors with a retry button', async () => {
	shell.getState.mockResolvedValue({ ...idle, checkedAt: '2026-09-10T01:00:00Z' });
	render(DesktopUpdatesSection);
	await screen.findByText('You’re up to date.');
	shell.listener?.({ ...idle, phase: 'error', error: 'Release feed is unavailable' });
	expect((await screen.findByRole('alert')).textContent).toBe('Release feed is unavailable');
	shell.check.mockRejectedValue(new Error('Could not reach desktop app'));
	await fireEvent.click(screen.getByRole('button', { name: 'Check for updates' }));
	await waitFor(() => expect(screen.getByRole('alert').textContent).toBe('Could not reach desktop app'));
	expect(screen.getByRole('button', { name: 'Check for updates' }).hasAttribute('disabled')).toBe(false);
});

it('disables checks on builds without updates', async () => {
	shell.getState.mockResolvedValue({ ...idle, phase: 'unsupported' });
	render(DesktopUpdatesSection);
	await screen.findByText(/In-app updates are disabled/);
	expect(screen.getByRole('button', { name: 'Check for updates' }).hasAttribute('disabled')).toBe(true);
	expect(shell.check).not.toHaveBeenCalled();
});

it('does not overwrite a live update with a delayed initial snapshot', async () => {
	let resolve!: (value: DesktopUpdateState) => void;
	shell.getState.mockReturnValue(new Promise<DesktopUpdateState>((done) => { resolve = done; }));
	render(DesktopUpdatesSection);
	shell.listener?.({ ...idle, phase: 'ready', version: '1.2.2' });
	await screen.findByRole('button', { name: 'Restart and update' });
	resolve(idle);
	await Promise.resolve();
	expect(screen.getByRole('button', { name: 'Restart and update' })).toBeTruthy();
});

it('hides desktop updates in a browser', () => {
	shell.available = false;
	render(DesktopUpdatesSection);
	expect(screen.queryByRole('heading', { name: 'AI Shuffle Desktop' })).toBeNull();
	expect(shell.getState).not.toHaveBeenCalled();
});
