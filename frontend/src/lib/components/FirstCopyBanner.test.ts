import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: mocks }));
import FirstCopyBanner from './FirstCopyBanner.svelte';

beforeEach(() => {
	vi.resetAllMocks();
	vi.useFakeTimers();
});
afterEach(() => {
	cleanup();
	vi.useRealTimers();
});

function status(bootstrap: 'ready' | 'syncing', firstCopy: object | null = null, transcripts: object | null = null) {
	return {
		device: { device_id: 'self', fingerprint: '' }, enrolled: true, platform: 'linux', version: '1', instance_id: 'i',
		network: { network_id: 'n', name: 'Home', device_id: 'self', fingerprint: '', founder_device_id: 'a',
			generation: 1, key_version: 1, bootstrap_state: bootstrap, member_count: 2 },
		hub: { linked: true, first_copy: firstCopy, transcripts }
	};
}

const downloading = (done: number) => ({
	peer: 'peer-a', peer_name: 'ServerWorker', phase: 'downloading', pages_done: done, pages_total: 22362,
	bytes_done: done * 400_000, bytes_total: 22362 * 400_000, error: null
});

const transcripts = (done: number, error: string | null = null) => ({
	phase: 'transcripts', peer: 'peer-a', peer_name: 'ServerWorker', sessions_done: done, sessions_total: 1152,
	events_copied: 0, error
});

it('stays silent and stops asking on a ready worker', async () => {
	mocks.get.mockResolvedValue(status('ready'));
	render(FirstCopyBanner);
	await vi.advanceTimersByTimeAsync(0);
	expect(mocks.get).toHaveBeenCalledWith('/network/status', { cache: 'no-store' });
	await vi.advanceTimersByTimeAsync(60_000);
	expect(mocks.get).toHaveBeenCalledTimes(1);
	expect(screen.queryByRole('status')).toBeNull();
});

it('shows copy progress, updates it, and offers a reload when the copy is done', async () => {
	mocks.get
		.mockResolvedValueOnce(status('syncing', downloading(12458)))
		.mockResolvedValueOnce(status('syncing', { ...downloading(22362), phase: 'applying' }))
		.mockResolvedValue(status('ready', { ...downloading(22362), phase: 'done' }));
	render(FirstCopyBanner);
	await vi.advanceTimersByTimeAsync(0);
	const banner = screen.getByRole('status');
	expect(banner.textContent).toContain('Copying your data from ServerWorker: 12,458 of 22,362 pages');
	expect(screen.queryByRole('button', { name: 'Minimize' })).toBeNull();
	expect(screen.getByRole('progressbar', { name: 'Copy progress' })).toBeTruthy();
	await vi.advanceTimersByTimeAsync(3000);
	expect(screen.getByRole('status').textContent).toContain('Saving the data copied from ServerWorker');
	expect(screen.queryByRole('progressbar')).toBeNull();
	await vi.advanceTimersByTimeAsync(3000);
	expect(screen.getByRole('status').textContent).toContain('Your data is copied');
	expect(screen.getByRole('button', { name: 'Reload' })).toBeTruthy();
	await vi.advanceTimersByTimeAsync(60_000);
	expect(mocks.get).toHaveBeenCalledTimes(3);
	await fireEvent.click(screen.getByRole('button', { name: /dismiss/i }));
	expect(screen.queryByRole('status')).toBeNull();
});

it('counts transcripts after the first copy and offers the reload with them', async () => {
	mocks.get
		.mockResolvedValueOnce(status('syncing', { ...downloading(60), pages_total: 60, phase: 'applying' }))
		.mockResolvedValueOnce(status('ready', { ...downloading(60), phase: 'done' }, transcripts(312)))
		.mockResolvedValue(status('ready', { ...downloading(60), phase: 'done' }));
	render(FirstCopyBanner);
	await vi.advanceTimersByTimeAsync(0);
	expect(screen.getByRole('status').textContent).toContain('Saving the data copied from ServerWorker');
	await vi.advanceTimersByTimeAsync(3000);
	expect(screen.getByRole('status').textContent).toContain(
		'Downloading transcripts from ServerWorker: 312 of 1,152 chats');
	expect(screen.getByRole('progressbar', { name: 'Copy progress' })).toBeTruthy();
	expect(screen.getByRole('button', { name: 'Reload' })).toBeTruthy();
	await fireEvent.click(screen.getByRole('button', { name: 'Minimize' }));
	const chip = screen.getByRole('button', { name: /restore details/i });
	expect(screen.queryByRole('status')).toBeNull();
	expect(screen.queryByRole('button', { name: 'Reload' })).toBeNull();
	await fireEvent.click(chip);
	expect(screen.getByRole('status').textContent).toContain('312 of 1,152 chats');
	expect(screen.getByRole('button', { name: 'Reload' })).toBeTruthy();
	await fireEvent.click(screen.getByRole('button', { name: 'Minimize' }));
	await vi.advanceTimersByTimeAsync(10_000);
	expect(screen.queryByRole('button', { name: /restore details/i })).toBeNull();
	expect(screen.getByRole('status').textContent).toContain('Your data is copied');
	expect(screen.getByRole('button', { name: 'Reload' })).toBeTruthy();
	await vi.advanceTimersByTimeAsync(60_000);
	expect(mocks.get).toHaveBeenCalledTimes(3);
});

it('keeps polling while minimized, restores the latest details, and clears on completion', async () => {
	mocks.get
		.mockResolvedValueOnce(status('ready', null, transcripts(312)))
		.mockResolvedValueOnce(status('ready', null, transcripts(576, 'HTTP 503')))
		.mockResolvedValue(status('ready'));
	render(FirstCopyBanner);
	await vi.advanceTimersByTimeAsync(0);
	const banner = screen.getByRole('status');
	expect(banner.textContent).toContain('312 of 1,152 chats');
	expect(screen.getByRole<HTMLProgressElement>('progressbar').value).toBe(312 / 1152);
	const minimize = screen.getByRole('button', { name: 'Minimize' });
	minimize.focus();
	await fireEvent.click(minimize);
	expect(screen.queryByRole('status')).toBeNull();
	expect(screen.queryByRole('progressbar')).toBeNull();
	expect(mocks.get).toHaveBeenCalledTimes(1);
	const chip = screen.getByRole('button', { name: /Downloading transcripts.*27%.*restore details/i });
	expect(document.activeElement).toBe(chip);

	await vi.advanceTimersByTimeAsync(10_000);
	expect(screen.getByRole('button', { name: /Paused.*50%.*restore details/i })).toBe(chip);
	await fireEvent.click(chip);
	const paused = screen.getByText(/paused at 576 of 1,152 chats: HTTP 503/);
	expect(document.activeElement).toBe(paused);
	expect(screen.getByRole<HTMLProgressElement>('progressbar').value).toBe(0.5);
	expect(screen.queryByRole('button', { name: /restore details/i })).toBeNull();

	await fireEvent.click(screen.getByRole('button', { name: 'Minimize' }));
	await vi.advanceTimersByTimeAsync(10_000);
	expect(screen.queryByRole('button', { name: /restore details/i })).toBeNull();
	expect(screen.queryByRole('status')).toBeNull();
	await vi.advanceTimersByTimeAsync(60_000);
	expect(mocks.get).toHaveBeenCalledTimes(3);
});

// The first-copy handoff adds Reload to the transcript details.
it.each(['chip', 'details', 'Minimize', 'Reload', 'composer', 'modal'] as const)(
	'handles completion while %s owns focus', async (owner) => {
		const workspace = document.createElement('section');
		workspace.tabIndex = -1;
		const composer = workspace.appendChild(document.createElement('textarea'));
		const modal = document.createElement('div');
		modal.setAttribute('role', 'dialog');
		modal.setAttribute('aria-modal', 'true');
		const modalAction = modal.appendChild(document.createElement('button'));
		document.body.append(workspace, modal);
		try {
			mocks.get
				.mockResolvedValueOnce(status('syncing', downloading(60)))
				.mockResolvedValueOnce(status('ready', null, transcripts(312)))
				.mockResolvedValue(status('ready'));
			render(FirstCopyBanner, { completionFocusTarget: workspace });
			await vi.advanceTimersByTimeAsync(3000);
			const minimize = screen.getByRole('button', { name: 'Minimize' });
			let focused: HTMLElement;
			if (owner === 'chip' || owner === 'details') {
				await fireEvent.click(minimize);
				focused = screen.getByRole('button', { name: /restore details/i });
				if (owner === 'details') {
					await fireEvent.click(focused);
					focused = screen.getByText(/312 of 1,152 chats/);
				}
			} else {
				focused = owner === 'composer' ? composer : owner === 'modal' ? modalAction
					: screen.getByRole('button', { name: owner });
				focused.focus();
			}
			expect(document.activeElement).toBe(focused);
			await vi.advanceTimersByTimeAsync(10_000);
			expect(screen.getByRole('status').textContent).toContain('Your data is copied');
			expect(document.activeElement).toBe(owner === 'composer' || owner === 'modal' ? focused : workspace);
		} finally {
			workspace.remove();
			modal.remove();
		}
	}
);
