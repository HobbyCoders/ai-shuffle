import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { WorkerMachine } from '$lib/api/worker-settings';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: mocks, getErrorMessage: (e: Error) => e.message }));
import WorkerMachineSettings from './WorkerMachineSettings.svelte';
import { MachineSettingsModel, signInText } from './machine-settings.svelte';

const worker = (device_id = 'unraid', extra: Partial<WorkerMachine> = {}): WorkerMachine => ({
	device_id, device_name: device_id === 'unraid' ? 'Unraid' : 'GamingPC', is_self: device_id === 'unraid', online: true,
	outcome: 'ok', reason: 'done', availability: { editable: true, always_on: true, always_on_override: null },
	status: {
		claude: { installed: true, version: '2.1.1', keep_installed: true, installed_by_app: true, can_install: true,
			signed_in: true, email: 'person@example.com', plan: 'Max', status_available: true, job: null },
		github_signed_in: false, release_channel: { channel: 'stable', editable: true, restart_required: false },
		claude_sign_in_supported: false
	}, ...extra
});
let workers: WorkerMachine[];
beforeEach(() => {
	vi.clearAllMocks();
	workers = [worker(), worker('gaming')];
	mocks.get.mockImplementation(async () => ({ workers }));
	mocks.post.mockImplementation(async () => ({ request_id: 'request', workers: workers.map((w) => ({
		device_id: w.device_id, device_name: w.device_name, outcome: 'accepted', reason: 'done', status: null
	})) }));
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

it('shows CLI-produced account/plan, version and GitHub status without remote sign-in actions', async () => {
	render(WorkerMachineSettings, { self: true });
	expect(await screen.findByText(/Signed in on Unraid · person@example.com · Max/)).toBeTruthy();
	expect(screen.getByText('2.1.1')).toBeTruthy();
	expect(screen.getByText('Not signed in on Unraid')).toBeTruthy();
	expect(screen.queryByRole('button', { name: /Sign in|Sign out/i })).toBeNull();
	expect(mocks.get).toHaveBeenCalledWith('/settings/worker-settings');
});

it('targets the selected worker, not the worker serving this UI', async () => {
	render(WorkerMachineSettings, { deviceId: 'gaming' });
	await fireEvent.click(await screen.findByRole('button', { name: 'Update Claude CLI' }));
	await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/settings/worker-settings/action', {
		target: 'gaming', action: { operation: 'claude-update' }
	}));
});

it('saves keep-installed and the per-worker release channel', async () => {
	render(WorkerMachineSettings, { self: true });
	await fireEvent.click(await screen.findByRole('switch', { name: 'Keep installed' }));
	await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/settings/worker-settings/action', {
		target: 'unraid', action: { operation: 'claude-keep-installed', keep_installed: false }
	}));
	await waitFor(() => expect(screen.getByRole('combobox', { name: 'Release channel' }).hasAttribute('disabled')).toBe(false));
	await fireEvent.change(screen.getByRole('combobox', { name: 'Release channel' }), { target: { value: 'nightly' } });
	await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/settings/worker-settings/action', {
		target: 'unraid', action: { operation: 'release-channel', channel: 'nightly' }
	}));
});

it('shows offline as offline, disables execution, but lets shared availability be edited', async () => {
	workers = [worker('gaming', { online: false, outcome: 'offline', reason: 'offline', status: null })];
	render(WorkerMachineSettings, { deviceId: 'gaming' });
	expect(await screen.findByText('GamingPC: Offline · not queued')).toBeTruthy();
	expect(screen.getByRole('button', { name: 'Install Claude CLI' }).hasAttribute('disabled')).toBe(true);
	await fireEvent.change(screen.getByRole('combobox', { name: 'Worker stays on' }), { target: { value: 'off' } });
	await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/settings/worker-settings/action', {
		target: 'gaming', action: { operation: 'always-on', always_on: false }
	}));
	expect(screen.queryByRole('alert')).toBeNull();
});

it('confirms an all-worker operation and reports each target, including uncertain and offline', async () => {
	mocks.post.mockResolvedValue({ request_id: 'r', workers: [
		{ device_id: 'unraid', device_name: 'Unraid', outcome: 'uncertain', reason: 'uncertain', status: null },
		{ device_id: 'gaming', device_name: 'GamingPC', outcome: 'offline', reason: 'offline', status: null }
	] });
	render(WorkerMachineSettings, { self: true });
	await screen.findByRole('button', { name: 'Update Claude CLI' });
	await fireEvent.click(screen.getByRole('button', { name: 'Apply…' }));
	expect(mocks.post).not.toHaveBeenCalled();
	await fireEvent.click(screen.getByRole('button', { name: 'Apply to all workers' }));
	await screen.findByText('Unraid: No confirmation · check status before retrying');
	expect(screen.getByText('GamingPC: Offline · not queued')).toBeTruthy();
	expect(mocks.post).toHaveBeenCalledExactlyOnceWith('/settings/worker-settings/action', {
		target: 'all', action: { operation: 'claude-install' }
	});
});

it('offers all-worker channel, keep-installed and shared stays-on operations', async () => {
	render(WorkerMachineSettings, { self: true });
	await screen.findByRole('button', { name: 'Update Claude CLI' });
	await fireEvent.change(screen.getByRole('combobox', { name: 'All workers action' }), { target: { value: 'nightly' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Apply…' }));
	await fireEvent.click(screen.getByRole('button', { name: 'Apply to all workers' }));
	await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/settings/worker-settings/action', {
		target: 'all', action: { operation: 'release-channel', channel: 'nightly' }
	}));
});

it('does not change a manually managed CLI or a launcher-controlled channel', async () => {
	workers[0].status!.claude!.installed_by_app = false;
	workers[0].status!.release_channel!.editable = false;
	render(WorkerMachineSettings, { self: true });
	expect((await screen.findByRole('button', { name: 'Update Claude CLI' })).hasAttribute('disabled')).toBe(true);
	expect(screen.getByRole('combobox', { name: 'Release channel' }).hasAttribute('disabled')).toBe(true);
});

it('coalesces concurrent loads and disables changes when the snapshot is stale', async () => {
	const model = new MachineSettingsModel();
	await Promise.all([model.load(), model.load()]);
	expect(mocks.get).toHaveBeenCalledTimes(1);
	mocks.get.mockRejectedValue(new Error('Unavailable'));
	await model.load();
	expect(model.workers).toHaveLength(2);
	expect(model.stale).toBe(true);
	expect(await model.act('unraid', { operation: 'claude-install' })).toBe(false);
	expect(mocks.post).not.toHaveBeenCalled();
});

it('polls only while visible and cleans up when the detail is closed', async () => {
	vi.useFakeTimers();
	const model = new MachineSettingsModel();
	const stop = model.start();
	await tick();
	vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
	await vi.advanceTimersByTimeAsync(10_000);
	expect(mocks.get).toHaveBeenCalledTimes(1);
	vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
	await vi.advanceTimersByTimeAsync(5000);
	expect(mocks.get).toHaveBeenCalledTimes(2);
	stop();
	await vi.advanceTimersByTimeAsync(10_000);
	expect(mocks.get).toHaveBeenCalledTimes(2);
});

it('never treats missing or failed status as a signed-out account', () => {
	expect(signInText(null)).toBe('Status unavailable');
	expect(signInText(undefined)).toBe('Status unavailable');
	expect(signInText(false)).toBe('Not signed in');
});
