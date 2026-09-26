import { beforeEach, afterEach, it, expect, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { Writable } from 'svelte/store';
import type { WorkerDeviceConfirmation } from '$lib/api/worker-device-confirmations';
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api, getErrorMessage: (e: Error) => e.message }));
vi.mock('$lib/stores/auth.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { isAdmin: writable(true) };
});
import { isAdmin } from '$lib/stores/auth.svelte';
import WorkerDeviceConfirmations from './WorkerDeviceConfirmations.svelte';
let devices: WorkerDeviceConfirmation[];
const device = (extra: Partial<WorkerDeviceConfirmation> = {}): WorkerDeviceConfirmation => ({
	device_id: 'device/a', hub_device_id: 'phone-origin-id', paired_client_id: 'paired-a', mobile_record_id: 'key-a',
	device_name: 'My phone', platform: 'android', confirmation: 'pending', revoked_at: null,
	api_key_expires_at: '2099-01-01T00:00:00Z', ...extra
});
beforeEach(() => {
	vi.resetAllMocks();
	(isAdmin as unknown as Writable<boolean>).set(true);
	devices = [device()];
	api.get.mockImplementation(async () => devices.map((d) => ({ ...d })));
	api.post.mockImplementation(async (_path, body) => {
		devices[0].confirmation = body.approved ? 'approved' : 'denied';
		return { confirmation: devices[0].confirmation };
	});
});
afterEach(cleanup);

it('shows identity and requires an explicit second action with the observed enrollment IDs', async () => {
	render(WorkerDeviceConfirmations);
	await screen.findByText('Awaiting worker approval');
	expect(screen.getByText('phone-origin-id')).not.toBeNull();
	expect(api.get).toHaveBeenCalledWith('/mobile/hub/devices/confirmations', { cache: 'no-store' });
	await fireEvent.click(screen.getByRole('button', { name: 'Approve My phone' }));
	expect(api.post).not.toHaveBeenCalled();
	await fireEvent.click(screen.getByRole('button', { name: 'Confirm approval' }));
	await screen.findByText('Approved');
	expect(api.post).toHaveBeenCalledExactlyOnceWith('/mobile/hub/devices/device%2Fa/confirmation', { paired_client_id: 'paired-a', mobile_record_id: 'key-a', approved: true });
});

it('lets the owner cancel without granting access', async () => {
	render(WorkerDeviceConfirmations);
	await fireEvent.click(await screen.findByRole('button', { name: 'Approve My phone' }));
	await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
	expect(api.post).not.toHaveBeenCalled();
	expect(screen.getByRole('button', { name: 'Approve My phone' })).not.toBeNull();
});

it('supports denial and does not offer to resurrect a denied identity', async () => {
	render(WorkerDeviceConfirmations);
	await fireEvent.click(await screen.findByRole('button', { name: 'Deny request' }));
	await fireEvent.click(screen.getByRole('button', { name: 'Confirm denial' }));
	await screen.findByText('Denied');
	expect(api.post).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ approved: false }));
	expect(screen.queryByRole('button', { name: /Approve My phone/ })).toBeNull();
});

it('requires confirmation to revoke previously approved worker access', async () => {
	devices = [device({ confirmation: 'approved' })];
	render(WorkerDeviceConfirmations);
	await fireEvent.click(await screen.findByRole('button', { name: 'Revoke worker access' }));
	expect(api.post).not.toHaveBeenCalled();
	await fireEvent.click(screen.getByRole('button', { name: 'Confirm denial' }));
	await screen.findByText('Denied');
});

it('never offers approval of revoked or expired credentials', async () => {
	devices = [device({ api_key_expires_at: '2020-01-01T00:00:00Z' }), device({ device_id: 'revoked', paired_client_id: 'p2', device_name: 'Revoked phone', revoked_at: '2026-01-01T00:00:00Z' })];
	render(WorkerDeviceConfirmations);
	expect(await screen.findByRole('button', { name: 'Approve My phone' })).toHaveProperty('disabled', true);
	expect(screen.queryByRole('button', { name: 'Approve Revoked phone' })).toBeNull();
});

it('keeps refusal visible and does not optimistically approve stale enrollment', async () => {
	api.post.mockRejectedValue(new Error('Device authorization changed'));
	render(WorkerDeviceConfirmations);
	await fireEvent.click(await screen.findByRole('button', { name: 'Approve My phone' }));
	await fireEvent.click(screen.getByRole('button', { name: 'Confirm approval' }));
	expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('Device authorization changed'));
	expect(screen.getByText('Awaiting worker approval')).not.toBeNull();
	expect(screen.getByRole('button', { name: 'Approve My phone' })).toHaveProperty('disabled', true);
	await fireEvent.click(screen.getByRole('button', { name: 'Refresh requests' }));
	await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
});

it('invalidates a pending choice when a refresh observes credential replacement', async () => {
	render(WorkerDeviceConfirmations);
	await fireEvent.click(await screen.findByRole('button', { name: 'Approve My phone' }));
	devices = [device({ mobile_record_id: 'replacement-key' })];
	await fireEvent.click(screen.getByRole('button', { name: 'Refresh requests' }));
	await waitFor(() => expect(screen.queryByRole('button', { name: 'Confirm approval' })).toBeNull());
	expect(api.post).not.toHaveBeenCalled();
});

it('explains worker-authentication refusal and permits an explicit retry', async () => {
	api.get.mockRejectedValueOnce(new Error('Sign in to the local worker or use an already-confirmed owner device'));
	render(WorkerDeviceConfirmations);
	expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('Sign in to the local worker'));
	expect(screen.queryByRole('button', { name: /Approve My phone/ })).toBeNull();
	await fireEvent.click(screen.getByRole('button', { name: 'Refresh requests' }));
	await screen.findByText('Awaiting worker approval');
});

it('does not read or expose admin decisions to a non-admin', async () => {
	(isAdmin as unknown as Writable<boolean>).set(false);
	render(WorkerDeviceConfirmations);
	expect(api.get).not.toHaveBeenCalled();
	expect(screen.queryByRole('heading')).toBeNull();
});

it('handles empty inventory without inventing an approval request', async () => {
	devices = [];
	render(WorkerDeviceConfirmations);
	await screen.findByText('No account device requests on this worker.');
	expect(api.post).not.toHaveBeenCalled();
});
