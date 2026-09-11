import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { DesktopUpdateState } from '$lib/services/desktop';

const shell = vi.hoisted(() => ({
	getState: vi.fn(), check: vi.fn(), download: vi.fn(), install: vi.fn(), unsubscribe: vi.fn(),
	listener: null as ((state: DesktopUpdateState) => void) | null, available: true
}));
vi.mock('$lib/services/desktop', () => ({
	get desktop() {
		return shell.available ? { updates: {
			getState: shell.getState, check: shell.check, download: shell.download, install: shell.install,
			onState: (listener: (state: DesktopUpdateState) => void) => { shell.listener = listener; return shell.unsubscribe; }
		} } : null;
	}
}));
import DesktopUpdateButton from './DesktopUpdateButton.svelte';
import DownloadsSection from '../../deck/cards/settings/DownloadsSection.svelte';

const idle: DesktopUpdateState = { phase: 'idle', version: null, percent: null, error: null, checkedAt: null };
const available: DesktopUpdateState = { ...idle, phase: 'available', version: 'abc123' };
beforeEach(() => { vi.resetAllMocks(); shell.available = true; shell.listener = null; shell.getState.mockResolvedValue(idle); });
afterEach(cleanup);

it.each(['idle', 'checking', 'unsupported', 'error'] as const)('stays hidden when %s without a known update', async (phase) => {
	shell.getState.mockResolvedValue({ ...idle, phase });
	render(DesktopUpdateButton);
	await waitFor(() => expect(shell.getState).toHaveBeenCalledOnce());
	expect(screen.queryByRole('button')).toBeNull();
	expect(shell.check).not.toHaveBeenCalled(); expect(shell.download).not.toHaveBeenCalled();
});

it('downloads on the first click and restarts only on a second click when ready', async () => {
	shell.getState.mockResolvedValue(available);
	const view = render(DesktopUpdateButton);
	const button = await screen.findByRole('button', { name: 'Download update' });
	expect(button.title).toBe('Update available. Click to download.');
	expect(shell.download).not.toHaveBeenCalled();
	shell.download.mockImplementation(async () => { shell.listener?.({ ...available, phase: 'downloading', percent: 38, message: 'Downloading source…' }); });
	await fireEvent.click(button);
	expect(shell.download).toHaveBeenCalledOnce(); expect(shell.check).not.toHaveBeenCalled();
	await waitFor(() => expect(button.title).toContain('38%'));
	expect(button.hasAttribute('disabled')).toBe(true);
	shell.listener?.({ ...available, phase: 'ready', percent: null });
	const restart = await screen.findByRole('button', { name: 'Update and restart' });
	expect(shell.install).not.toHaveBeenCalled();
	await fireEvent.click(restart); expect(shell.install).toHaveBeenCalledOnce();
	view.unmount(); expect(shell.unsubscribe).toHaveBeenCalledOnce();
});

it('blocks repeat clicks while the download invocation is pending', async () => {
	shell.getState.mockResolvedValue(available);
	let complete!: () => void;
	shell.download.mockReturnValue(new Promise<void>((resolve) => { complete = resolve; }));
	render(DesktopUpdateButton);
	const button = await screen.findByRole('button', { name: 'Download update' });
	await fireEvent.click(button); await fireEvent.click(button);
	expect(shell.download).toHaveBeenCalledOnce(); expect(button.hasAttribute('disabled')).toBe(true);
	complete();
});

it('lets a failed download retry without automatically restarting', async () => {
	shell.getState.mockResolvedValue({ ...available, phase: 'error', error: 'Network unavailable' });
	render(DesktopUpdateButton);
	const button = await screen.findByRole('button', { name: 'Download update' });
	expect(button.title).toContain('Network unavailable');
	shell.download.mockRejectedValue(new Error('Desktop connection failed'));
	await fireEvent.click(button);
	await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Desktop connection failed'));
	expect(button.hasAttribute('disabled')).toBe(false); expect(shell.install).not.toHaveBeenCalled();
});

it('retries a failed install directly without another download', async () => {
	shell.getState.mockResolvedValue({ ...available, phase: 'ready', error: 'Worker could not stop' });
	shell.install.mockRejectedValue(new Error('Please retry the restart'));
	render(DesktopUpdateButton);
	await fireEvent.click(await screen.findByRole('button', { name: 'Update and restart' }));
	await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Please retry the restart'));
	expect(shell.install).toHaveBeenCalledOnce(); expect(shell.download).not.toHaveBeenCalled();
});

it('labels a metadata recheck without claiming a download is running', async () => {
	shell.getState.mockResolvedValue({ ...available, phase: 'checking' });
	render(DesktopUpdateButton);
	const button = await screen.findByRole('button', { name: 'Checking for updates' });
	expect(button.hasAttribute('disabled')).toBe(true);
	expect(shell.download).not.toHaveBeenCalled();
});

it('does not overwrite an event with a stale initial snapshot', async () => {
	let resolve!: (value: DesktopUpdateState) => void;
	shell.getState.mockReturnValue(new Promise<DesktopUpdateState>((done) => { resolve = done; }));
	render(DesktopUpdateButton);
	shell.listener?.({ ...available, phase: 'ready' });
	await screen.findByRole('button', { name: 'Update and restart' });
	resolve(idle); await Promise.resolve();
	expect(screen.getByRole('button', { name: 'Update and restart' })).toBeTruthy();
});

it('stays hidden in an ordinary browser', () => {
	shell.available = false; render(DesktopUpdateButton);
	expect(screen.queryByRole('button')).toBeNull(); expect(shell.getState).not.toHaveBeenCalled();
});

it('keeps Android downloads in Settings without a second desktop update control', () => {
	render(DownloadsSection);
	expect(screen.getByRole('link', { name: 'Download latest APK' })).toBeTruthy();
	expect(screen.queryByRole('button')).toBeNull(); expect(shell.getState).not.toHaveBeenCalled();
});
