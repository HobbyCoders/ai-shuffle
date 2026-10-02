/**
 * Approval requests, ported from WorkerDeviceConfirmations.test.ts. The
 * component-level confirm steps (Approve > "IDs match?" > Approve) are
 * covered in DevicesCollection.test.ts.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { Writable } from 'svelte/store';
import type { WorkerDeviceConfirmation } from '$lib/api/worker-device-confirmations';

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api, getErrorMessage: (e: Error) => e.message }));
vi.mock('$lib/stores/auth.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { isAdmin: writable(true) };
});
import { isAdmin } from '$lib/stores/auth.svelte';
import { DeviceRequests, isPendingRequest, requestId, requestStatusText } from './device-requests.svelte';

let devices: WorkerDeviceConfirmation[];
const device = (extra: Partial<WorkerDeviceConfirmation> = {}): WorkerDeviceConfirmation => ({
	device_id: 'device/a', hub_device_id: 'phone-origin-id', paired_client_id: 'paired-a', mobile_record_id: 'key-a',
	device_name: 'My phone', platform: 'android', confirmation: 'pending', revoked_at: null,
	api_key_expires_at: '2099-01-01T00:00:00Z', ...extra
});
let requests: DeviceRequests;

beforeEach(() => {
	vi.resetAllMocks();
	(isAdmin as unknown as Writable<boolean>).set(true);
	devices = [device()];
	api.get.mockImplementation(async () => devices.map((d) => ({ ...d })));
	api.post.mockImplementation(async (_path, body) => {
		devices[0].confirmation = body.approved ? 'approved' : 'denied';
		return { confirmation: devices[0].confirmation };
	});
	requests = new DeviceRequests();
});
afterEach(() => { requests.reset(); vi.useRealTimers(); });

async function loaded() {
	requests.start();
	await vi.waitFor(() => expect(requests.loaded).toBe(true));
}

it('reads without caching and approves only with the observed enrollment ids', async () => {
	await loaded();
	expect(api.get).toHaveBeenCalledWith('/mobile/hub/devices/confirmations', { cache: 'no-store' });
	expect(requestStatusText(requests.devices[0])).toBe('Wants to be approved');
	expect(requests.pendingCount).toBe(1);
	await requests.decide();
	expect(api.post).not.toHaveBeenCalled();
	requests.arm(requests.devices[0], true);
	await requests.decide();
	expect(api.post).toHaveBeenCalledExactlyOnceWith('/mobile/hub/devices/device%2Fa/confirmation', { paired_client_id: 'paired-a', mobile_record_id: 'key-a', approved: true });
	expect(requests.notice).toContain('Approved on this worker');
	expect(requestStatusText(requests.devices[0])).toBe('Approved');
	expect(requests.pendingCount).toBe(0);
});

it('cancelling grants nothing', async () => {
	await loaded();
	requests.arm(requests.devices[0], true);
	requests.disarm();
	await requests.decide();
	expect(api.post).not.toHaveBeenCalled();
});

it('denies, and a denied identity is no longer pending', async () => {
	await loaded();
	requests.arm(requests.devices[0], false);
	await requests.decide();
	expect(api.post).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ approved: false }));
	expect(requestStatusText(requests.devices[0])).toBe('Denied');
	expect(isPendingRequest(requests.devices[0])).toBe(false);
});

it('never counts revoked or expired credentials as pending', () => {
	expect(isPendingRequest(device({ api_key_expires_at: '2020-01-01T00:00:00Z' }))).toBe(false);
	expect(isPendingRequest(device({ revoked_at: '2026-01-01T00:00:00Z' }))).toBe(false);
	expect(requestStatusText(device({ revoked_at: '2026-01-01T00:00:00Z' }))).toBe('Revoked');
});

it('keeps a refusal visible, blocks decisions, and clears on an explicit refresh', async () => {
	api.post.mockRejectedValue(new Error('Device authorization changed'));
	await loaded();
	requests.arm(requests.devices[0], true);
	await requests.decide();
	expect(requests.error).toBe('Device authorization changed');
	expect(requests.devices[0].confirmation).toBe('pending');
	requests.arm(requests.devices[0], true);
	await requests.decide();
	expect(api.post).toHaveBeenCalledOnce();
	await requests.refresh();
	expect(requests.error).toBe('');
});

it('disarms when a refresh shows a replaced credential', async () => {
	await loaded();
	requests.arm(requests.devices[0], true);
	devices = [device({ mobile_record_id: 'replacement-key' })];
	await requests.refresh();
	expect(requests.armed).toBeNull();
	await requests.decide();
	expect(api.post).not.toHaveBeenCalled();
});

it('surfaces a worker-authentication refusal and retries on request', async () => {
	api.get.mockRejectedValueOnce(new Error('Sign in to the local worker or use an already-confirmed owner device'));
	requests.start();
	await vi.waitFor(() => expect(requests.error).toContain('Sign in to the local worker'));
	await requests.refresh();
	expect(requests.loaded).toBe(true);
	expect(requests.error).toBe('');
});

it('does not read decisions for an unapproved viewer', async () => {
	(isAdmin as unknown as Writable<boolean>).set(false);
	requests.start();
	await Promise.resolve();
	expect(api.get).not.toHaveBeenCalled();
});

it('handles an empty inventory', async () => {
	devices = [];
	await loaded();
	expect(requests.devices).toEqual([]);
	expect(requests.pendingCount).toBe(0);
});

it('polls every 10s only while nothing is armed', async () => {
	vi.useFakeTimers();
	requests.start();
	await vi.advanceTimersByTimeAsync(0);
	expect(api.get).toHaveBeenCalledTimes(1);
	await vi.advanceTimersByTimeAsync(10_000);
	expect(api.get).toHaveBeenCalledTimes(2);
	requests.arm(requests.devices[0], true);
	await vi.advanceTimersByTimeAsync(30_000);
	expect(api.get).toHaveBeenCalledTimes(2);
	expect(requestId(requests.devices[0])).toBe('req:device/a:paired-a');
});
