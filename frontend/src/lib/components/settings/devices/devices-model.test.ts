/**
 * Devices model, ported from DevicesSection.test.ts: the device inventory,
 * joining devices, account situations, renames, revokes (account first,
 * scoped retry), legacy installs, scoped load failures, polling and
 * resource events.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { Install } from '$lib/api/installs';
import type { PairedClient } from '$lib/api/pairing';
import type { NetworkMember, NetworkStatus } from '$lib/api/network';

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() }));
const appData = vi.hoisted(() => ({ dropDeviceSessions: vi.fn() }));
const banner = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', () => ({ api, getErrorMessage: (e: Error) => e.message }));
vi.mock('$lib/stores/app-data.svelte', () => ({ appData }));
vi.mock('$lib/stores/notifications', () => ({ notify: { banner } }));
vi.mock('$lib/stores/auth.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { isAdmin: writable(true), auth: { current: { status: { auth_mode: 'local' } }, logout: vi.fn() } };
});

import { DevicesModel, clientKind } from './devices-model.svelte';
import { resourceSync } from '$lib/services/resource-sync';

const fingerprint = 'abcd-'.repeat(7) + 'abcd';
const member = (device_id: string, extra: Partial<NetworkMember> = {}): NetworkMember => ({
	device_id,
	name: 'Laptop',
	kind: 'worker',
	platform: 'macos',
	status: 'active',
	generation: 1,
	is_self: false,
	fingerprint,
	added_by: 'self',
	created_at: null,
	last_seen: '2026-09-01T10:00:00Z',
	endpoints: [],
	capabilities: {},
	version: '1.0',
	online: true,
	legacy_instance_id: null,
	...extra
});
const install = (id: string, extra: Partial<Install> = {}): Install => ({
	id,
	device_name: 'Shared worker',
	base_url: null,
	platform: 'linux',
	version: '1.0',
	capabilities: {},
	last_seen: null,
	online: true,
	approved: true,
	...extra
});
const client = (id: string, extra: Partial<PairedClient> = {}): PairedClient => ({
	id,
	device_id: id,
	device_name: 'My phone',
	platform: 'android',
	client_type: 'mobile',
	scopes: ['controller'],
	session_expires_at: null,
	paired_at: '2026-09-01T10:00:00Z',
	last_seen: null,
	revoked_at: null,
	created_at: '',
	updated_at: '',
	...extra
});
const enrolled = (): NetworkStatus => ({
	device: { device_id: 'self', fingerprint },
	enrolled: true,
	platform: 'linux',
	version: '1.0',
	instance_id: 'self',
	network: {
		network_id: 'n',
		name: 'Home',
		device_id: 'self',
		fingerprint,
		founder_device_id: 'self',
		generation: 1,
		key_version: 1,
		bootstrap_state: 'ready',
		member_count: 2
	},
	members: [member('self', { name: 'Desk', is_self: true, platform: 'linux' }), member('laptop')],
	pending_joins: [],
	sync: { records: {}, ops: 3, holes: [], peers: [], conflicts: 0 },
	recovery: { configured: false, created_at: null, key_version: null },
	hub: { linked: false }
});
let status: NetworkStatus;
let installs: Install[];
let clients: PairedClient[];
let connection: unknown;
let profile: unknown;
const disconnected = { signed_in: false, status: 'disconnected', code: null, qr_svg: null, error: null };
function reads(path: string): unknown {
	if (path === '/network/status') return status;
	if (path === '/cluster/installs') return { installs: [...installs], self_instance_id: 'self' };
	if (path === '/pairing/clients') return { clients: [...clients] };
	if (path === '/pairing/endpoints') return { endpoints: [] };
	if (path === '/mobile/hub/connection') return connection;
	if (path === '/mobile/hub/account') return structuredClone(profile);
	if (path === '/mobile/hub/devices/confirmations') return [];
	throw new Error('Unexpected GET ' + path);
}
let model: DevicesModel;
let stop: () => void;

beforeEach(() => {
	vi.resetAllMocks();
	status = enrolled();
	installs = [
		install('self', { device_id: 'self', is_self: true, device_name: 'Desk' }),
		install('laptop', { device_id: 'laptop', device_name: 'Laptop' })
	];
	clients = [
		client('phone'),
		client('browser', { device_name: 'Work browser', client_type: 'browser', platform: 'linux-browser' })
	];
	connection = disconnected;
	profile = null;
	api.get.mockImplementation(async (path: string) => reads(path));
	model = new DevicesModel();
});
afterEach(() => {
	stop?.();
	model.reset();
	resourceSync.clear();
	vi.useRealTimers();
});

async function started() {
	stop = model.start();
	await vi.waitFor(() => expect(model.complete).toBe(true));
}

it('counts workers, phones and browsers in one inventory without advanced data', async () => {
	await started();
	expect(model.total).toBe(4);
	expect(model.uiState).toBe('ready');
	expect(model.workerName).toBe('Desk');
	expect(model.members.map((m) => m.device_id)).toEqual(['laptop']);
	expect(model.legacyInstalls).toEqual([]);
	expect(clientKind(model.clients[0])).toBe('Phone');
	expect(clientKind(model.clients[1])).toBe('Browser');
	expect(
		api.get.mock.calls.some(
			([path]) => ['/cluster/advertise', '/network/conflicts'].includes(path) || path.includes('/secret')
		)
	).toBe(false);
});

it('explains startup sync that opened saved data', async () => {
	status.startup_sync = { status: 'offline', reason: 'Other workers are offline. Showing saved data.' };
	await started();
	expect(model.startupSyncNote).toBe('Other workers are offline. Showing saved data.');
});

it('renames this worker and revokes another worker, with the rotate-keys toast', async () => {
	api.patch.mockResolvedValue({});
	api.delete.mockResolvedValue({
		device_id: 'laptop',
		generation: 2,
		key_version: 2,
		rotate_provider_keys_recommended: true
	});
	await started();
	expect(await model.rename('self', '  Studio desk ')).toBe(true);
	expect(api.patch).toHaveBeenCalledWith('/network/members/self', { name: 'Studio desk' });
	expect(await model.revoke('member:laptop', 'Laptop')).toBe(true);
	expect(api.delete).toHaveBeenCalledWith('/network/members/laptop');
	expect(banner).toHaveBeenCalledOnce();
	const [message, opts] = banner.mock.calls[0];
	expect(message).toContain('Laptop revoked');
	expect(message).toContain('Rotate');
	expect(opts.action.label).toBe('Open providers');
});

it('lists devices joining right now, not an approval queue', async () => {
	status = {
		...enrolled(),
		pending_joins: [
			{
				id: 'j1',
				status: 'pending',
				device_id: 'dev_new',
				name: 'New laptop',
				platform: 'windows',
				fingerprint,
				legacy_instance_id: null,
				created_at: null,
				expires_at: null
			}
		],
		hub: {
			linked: true,
			registered: true,
			status: 'active',
			pending_devices: [{ device_id: 'dev_phone', name: 'Office PC', platform: 'windows', signing_public: 'x' }]
		}
	};
	await started();
	expect(model.joining).toEqual([
		{ key: 'join:j1', name: 'New laptop', platform: 'windows', via: 'invite' },
		{ key: 'hub:dev_phone', name: 'Office PC', platform: 'windows', via: 'account' }
	]);
});

it('waits for an account worker without offering cloud restoration', async () => {
	status = {
		device: { device_id: 'self', fingerprint },
		enrolled: false,
		platform: 'linux',
		version: '1.0',
		instance_id: 'self',
		enrollment: null,
		hub: {
			linked: true,
			registered: false,
			status: 'pending',
			account_network: {
				network_id: 'n',
				name: null,
				founder_device_id: 'other',
				devices: [{ device_id: 'other', name: 'Office PC', platform: 'windows', kind: 'worker', status: 'active' }],
				recovery_available: true
			}
		}
	};
	installs = [];
	await started();
	expect(model.waitingOnAccount).toBe(true);
	expect(model.uiState).toBe('waiting');
	expect(model.accountWorkers.map((d) => d.name)).toEqual(['Office PC']);
	expect(api.post).not.toHaveBeenCalled();
});

it('switches to the account settings by itself; only a failed switch retries through adopt', async () => {
	const account_network = {
		network_id: 'other',
		name: null,
		founder_device_id: 'x',
		devices: [{ device_id: 'x', name: 'Office PC', platform: 'windows', kind: 'worker', status: 'active' }],
		recovery_available: false
	};
	status = { ...enrolled(), hub: { linked: true, registered: false, status: 'conflict', account_network } };
	api.post.mockResolvedValue({ adopted: true, status: 'pending' });
	await started();
	expect(model.switchingToAccount).toBe(true);
	expect(model.uiState).toBe('waiting');
	await model.adopt();
	expect(api.post).toHaveBeenCalledWith('/network/hub/adopt', {});
});

it('keeps separate settings by founding its own network', async () => {
	status = {
		device: { device_id: 'self', fingerprint },
		enrolled: false,
		platform: 'linux',
		version: '1.0',
		instance_id: 'self',
		enrollment: null
	};
	installs = [install('self', { is_self: true, device_name: 'Desk' })];
	api.post.mockResolvedValue({ network: { name: null } });
	stop = model.start();
	await vi.waitFor(() => expect(model.status).not.toBeNull());
	await model.startFresh();
	expect(api.post).toHaveBeenCalledWith('/network/create', {});
});

it('revokes a paired client through the client API and drops it', async () => {
	await started();
	expect(await model.revoke('client:phone', 'My phone')).toBe(true);
	expect(api.delete).toHaveBeenCalledWith('/pairing/clients/phone');
	expect(model.clients.map((c) => c.id)).toEqual(['browser']);
	expect(model.total).toBe(3);
	expect(appData.dropDeviceSessions).not.toHaveBeenCalled();
});

it('keeps legacy shared-database installs with allow, revoke, rename and remove', async () => {
	installs.push(install('pending', { device_name: 'Old runner', approved: false }));
	api.put.mockImplementation(async (_path: string, body: { approved: boolean }) =>
		install('pending', { device_name: 'Old runner', approved: body.approved })
	);
	api.patch.mockResolvedValue(install('pending', { device_name: 'Renamed runner' }));
	await started();
	expect(model.total).toBe(5);
	await model.approveLegacy(model.legacyInstalls[0], true);
	expect(api.put).toHaveBeenCalledWith('/cluster/installs/pending/approval', { approved: true });
	await model.rename('legacy:pending', 'Renamed runner');
	expect(api.patch).toHaveBeenCalledWith('/cluster/installs/pending', { device_name: 'Renamed runner' });
	expect(model.legacyInstalls[0].device_name).toBe('Renamed runner');
	await model.revoke('legacy:pending', 'Renamed runner', 'revoke');
	expect(api.put).toHaveBeenLastCalledWith('/cluster/installs/pending/approval', { approved: false });
	await model.revoke('legacy:pending', 'Renamed runner', 'remove');
	expect(api.delete).toHaveBeenCalledWith('/cluster/installs/pending');
	expect(appData.dropDeviceSessions).toHaveBeenCalledWith('pending');
	expect(model.legacyInstalls).toEqual([]);
});

it.each(['/cluster/installs', '/pairing/clients'])(
	'keeps healthy devices when %s fails and retries that source alone',
	async (failedPath) => {
		api.get.mockImplementation(async (path: string) => {
			if (path === failedPath) throw new Error('Source unavailable');
			return reads(path);
		});
		stop = model.start();
		await vi.waitFor(() => expect(model.workersError || model.clientsError).toBe('Source unavailable'));
		expect(model.status).not.toBeNull();
		api.get.mockImplementation(async (path: string) => reads(path));
		if (failedPath === '/cluster/installs') await model.loadWorkers();
		else await model.loadClients();
		expect(model.workersError || model.clientsError).toBe('');
		expect(model.total).toBe(4);
	}
);

it('treats refused reads (unapproved device) as forbidden, not errors', async () => {
	api.get.mockImplementation(async (path: string) => {
		if (['/network/status', '/cluster/installs', '/pairing/clients'].includes(path))
			throw { status: 403, detail: 'Admin only' };
		return reads(path);
	});
	stop = model.start();
	await vi.waitFor(() => expect(model.forbidden).toBe(true));
	expect(model.statusError).toBe('');
	expect(model.workersError).toBe('');
	expect(model.clientsError).toBe('');
});

it('reloads on network events and stops polling once every view is gone', async () => {
	vi.useFakeTimers();
	stop = model.start();
	await vi.advanceTimersByTimeAsync(0);
	status = { ...enrolled(), members: [...enrolled().members!, member('tablet', { name: 'Tablet' })] };
	api.get.mockClear();
	resourceSync.invalidate(['network']);
	await vi.advanceTimersByTimeAsync(100);
	expect(model.members.map((m) => m.name)).toContain('Tablet');
	expect(api.get.mock.calls.some(([path]) => path === '/network/status')).toBe(true);
	// A second view keeps it alive; only the last stop ends polling.
	const second = model.start();
	stop();
	const before = api.get.mock.calls.length;
	await vi.advanceTimersByTimeAsync(10_000);
	expect(api.get.mock.calls.length).toBeGreaterThan(before);
	second();
	const count = api.get.mock.calls.length;
	await vi.advanceTimersByTimeAsync(20_000);
	expect(api.get).toHaveBeenCalledTimes(count);
});

it('merges known local identities and keeps offline account devices', async () => {
	status.members![1].endpoints = ['https://laptop.example/'];
	connection = { ...disconnected, signed_in: true, status: 'connected' };
	profile = {
		account: { id: 'account', username: 'owner', profile_complete: true },
		servers: [
			{ id: 'server-self', name: 'Desk account name', current: true, url: null },
			{ id: 'server-laptop', name: 'Laptop account name', current: false, url: 'https://laptop.example' },
			{ id: 'offline', name: 'Offline PC', current: false, url: null }
		],
		devices: [
			{ id: 'account-phone', local_device_id: 'phone', name: 'Phone account name', platform: 'android' },
			{ id: 'offline-phone', name: 'Offline phone', platform: 'ios' },
			{ id: 'same-name', name: 'Laptop', platform: 'web' }
		]
	};
	stop = model.start();
	await vi.waitFor(() => expect(model.account.profile).not.toBeNull());
	expect(model.accountDevices.map((d) => [d.key, d.kind])).toEqual([
		['account-worker:offline', 'Worker'],
		['account-device:offline-phone', 'Phone'],
		['account-device:same-name', 'Browser']
	]);
	expect(model.total).toBe(7);
	expect(model.linkedRegistration('member:laptop')?.id).toBe('server-laptop');
	expect(model.linkedRegistration('client:phone')?.id).toBe('account-phone');
});

it.each(['servers', 'devices'] as const)(
	'revokes an account-only %s registration and refreshes the inventory',
	async (kind) => {
		connection = { ...disconnected, signed_in: true, status: 'connected' };
		profile = {
			account: { id: 'a', username: 'owner', profile_complete: true },
			servers: [{ id: 'offline', name: 'Offline PC', current: false, url: null }],
			devices: [{ id: 'offline', name: 'Offline phone', platform: 'android' }]
		};
		stop = model.start();
		await vi.waitFor(() => expect(model.accountDevices).toHaveLength(2));
		const key = kind === 'servers' ? 'account-worker:offline' : 'account-device:offline';
		api.delete.mockRejectedValueOnce(new Error('Account service unavailable'));
		expect(await model.revoke(key, 'x')).toBe(false);
		expect(model.error).toBe('Account service unavailable');
		expect(model.accountDevices).toHaveLength(2);
		api.delete.mockResolvedValueOnce({ ok: true });
		profile = { ...(profile as object), [kind]: [] };
		expect(await model.revoke(key, 'x')).toBe(true);
		expect(api.delete).toHaveBeenLastCalledWith(`/mobile/hub/account/${kind}/offline`);
		expect(model.accountDevices).toHaveLength(1);
		expect(api.get.mock.calls.filter(([p]) => p === '/mobile/hub/account').length).toBeGreaterThan(1);
	}
);

it('never revokes the current worker registration', async () => {
	connection = { ...disconnected, signed_in: true, status: 'connected' };
	profile = {
		account: { id: 'a', username: 'owner', profile_complete: true },
		servers: [{ id: 'me', name: 'Desk', current: true, url: null }],
		devices: []
	};
	stop = model.start();
	await vi.waitFor(() => expect(model.account.profile).not.toBeNull());
	expect(await model.revoke('self', 'Desk')).toBe(false);
	expect(api.delete).not.toHaveBeenCalled();
});

it.each(['phone', 'member', 'legacy'] as const)('revokes account then local access for a linked %s', async (kind) => {
	status.members![1].endpoints = ['https://laptop.example/'];
	if (kind === 'legacy') installs.push(install('old', { device_name: 'Old worker', base_url: 'https://old.example' }));
	const isPhone = kind === 'phone';
	connection = { ...disconnected, signed_in: true, status: 'connected' };
	profile = {
		account: { id: 'account', username: 'owner', profile_complete: true },
		servers: isPhone
			? []
			: [
					{
						id: 'linked',
						name: 'Account worker',
						current: false,
						url: kind === 'member' ? 'https://laptop.example' : 'https://old.example'
					}
				],
		devices: isPhone ? [{ id: 'linked', local_device_id: 'phone', name: 'Account phone', platform: 'android' }] : []
	};
	const events: string[] = [];
	api.delete.mockImplementation(async (path: string) => {
		events.push(path);
		return { ok: true, rotate_provider_keys_recommended: true };
	});
	api.put.mockImplementation(async (path: string) => {
		events.push(path);
		return install('old', { approved: false });
	});
	stop = model.start();
	await vi.waitFor(() => expect(model.account.profile).not.toBeNull());
	await vi.waitFor(() => expect(model.workersLoaded && model.clientsLoaded).toBe(true));
	const key = isPhone ? 'client:phone' : kind === 'member' ? 'member:laptop' : 'legacy:old';
	expect(model.linkedRegistration(key)).toBeTruthy();
	expect(await model.revoke(key, 'Device')).toBe(true);
	expect(events).toEqual([
		`/mobile/hub/account/${isPhone ? 'devices' : 'servers'}/linked`,
		isPhone
			? '/pairing/clients/phone'
			: kind === 'member'
				? '/network/members/laptop'
				: '/cluster/installs/old/approval'
	]);
});

it('stops on account failure and lets local cleanup retry after the account part succeeded', async () => {
	connection = { ...disconnected, signed_in: true, status: 'connected' };
	profile = {
		account: { id: 'account', username: 'owner', profile_complete: true },
		servers: [],
		devices: [{ id: 'linked', local_device_id: 'phone', name: 'Account phone', platform: 'android' }]
	};
	stop = model.start();
	await vi.waitFor(() => expect(model.linkedRegistration('client:phone')).toBeTruthy());
	api.delete.mockRejectedValueOnce(new Error('Account service unavailable'));
	expect(await model.revoke('client:phone', 'My phone')).toBe(false);
	expect(api.delete.mock.calls).toEqual([['/mobile/hub/account/devices/linked']]);
	api.delete.mockResolvedValueOnce({ ok: true });
	api.delete.mockRejectedValueOnce(new Error('Local cleanup failed'));
	expect(await model.revoke('client:phone', 'My phone')).toBe(false);
	expect(model.error).toContain('Account access was revoked. Retry');
	api.delete.mockResolvedValue({ ok: true });
	expect(await model.revoke('client:phone', 'My phone')).toBe(true);
	expect(model.clients.map((c) => c.id)).toEqual(['browser']);
});
