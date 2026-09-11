import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import type { Install } from '$lib/api/installs';
import type { PairedClient } from '$lib/api/pairing';
import type { NetworkMember, NetworkStatus } from '$lib/api/network';

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() }));
const appData = vi.hoisted(() => ({ dropDeviceSessions: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api, getErrorMessage: (e: Error) => e.message }));
vi.mock('$lib/stores/app-data.svelte', () => ({ appData }));
vi.mock('$lib/stores/auth.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { isAdmin: writable(true), auth: { current: { status: { auth_mode: 'local' } }, logout: vi.fn() } };
});
import DevicesSection from './DevicesSection.svelte';
import { resourceSync } from '$lib/services/resource-sync';

const fingerprint = 'abcd-'.repeat(7) + 'abcd';
const member = (device_id: string, extra: Partial<NetworkMember> = {}): NetworkMember => ({ device_id, name: 'Laptop', kind: 'worker', platform: 'macos', status: 'active', generation: 1, is_self: false, fingerprint, added_by: 'self', created_at: null, last_seen: '2026-09-01T10:00:00Z', endpoints: [], capabilities: {}, version: '1.0', online: true, legacy_instance_id: null, ...extra });
const install = (id: string, extra: Partial<Install> = {}): Install => ({ id, device_name: 'Shared worker', base_url: null, platform: 'linux', version: '1.0', capabilities: {}, last_seen: null, online: true, approved: true, ...extra });
const client = (id: string, extra: Partial<PairedClient> = {}): PairedClient => ({ id, device_id: id, device_name: 'My phone', platform: 'android', client_type: 'mobile', scopes: ['controller'], session_expires_at: null, paired_at: '2026-09-01T10:00:00Z', last_seen: null, revoked_at: null, created_at: '', updated_at: '', ...extra });
const enrolled = (): NetworkStatus => ({
	device: { device_id: 'self', fingerprint }, enrolled: true, platform: 'linux', version: '1.0', instance_id: 'self',
	network: { network_id: 'n', name: 'Home', device_id: 'self', fingerprint, founder_device_id: 'self', generation: 1, key_version: 1, bootstrap_state: 'ready', member_count: 2 },
	members: [member('self', { name: 'Desk', is_self: true, platform: 'linux' }), member('laptop')],
	pending_joins: [], sync: { records: {}, ops: 3, holes: [], peers: [], conflicts: 0 }, recovery: { configured: false, created_at: null, key_version: null }, hub: { linked: false }
});
let status: NetworkStatus;
let installs: Install[];
let clients: PairedClient[];
const advertise = { url: 'http://10.0.0.5:8000', source: 'guess', env_pinned: false, override: null, instance_id: 'self' };
const disconnected = { signed_in: false, status: 'disconnected', code: null, qr_svg: null, error: null };
function reads(path: string): unknown {
	if (path === '/network/status') return status;
	if (path === '/cluster/installs') return { installs: [...installs], self_instance_id: 'self' };
	if (path === '/pairing/clients') return { clients: [...clients] };
	if (path === '/cluster/advertise') return advertise;
	if (path === '/pairing/endpoints') return { endpoints: [{ url: 'http://10.0.0.5:8000', kind: 'lan', label: 'Local network', secure: false, loopback: false, qr_eligible: true, source: 'advertised' }] };
	if (path === '/mobile/hub/connection') return disconnected;
	if (path === '/network/conflicts') return { conflicts: [] };
	throw new Error('Unexpected GET ' + path);
}
beforeEach(() => {
	vi.resetAllMocks();
	status = enrolled();
	installs = [install('self', { device_id: 'self', is_self: true, device_name: 'Desk' }), install('laptop', { device_id: 'laptop', device_name: 'Laptop' })];
	clients = [client('phone'), client('browser', { device_name: 'Work browser', client_type: 'browser', platform: 'linux-browser' })];
	api.get.mockImplementation(async (path: string) => reads(path));
});
afterEach(() => { cleanup(); resourceSync.clear(); vi.useRealTimers(); });
it('defaults to peer sync and only enables cloud backup on an explicit click', async () => {
	status.hub = { linked: true, backup_enabled: false, sync_mode: 'peers', remote_durability: 'local_only' };
	api.put.mockResolvedValue({ linked: true, backup_enabled: true, remote_durability: 'pending', backlog: 3 });
	render(DevicesSection);
	expect(await screen.findByText('Sync directly between workers')).toBeTruthy();
	expect(screen.queryByText('Cloud backup up to date')).toBeNull();
	expect(api.put).not.toHaveBeenCalled();
	await fireEvent.click(screen.getByRole('button', { name: 'Enable cloud backup on this worker' }));
	expect(api.put).toHaveBeenCalledWith('/network/backup', { enabled: true });
	expect(await screen.findByText('Saved on this worker · cloud backup pending')).toBeTruthy();
});
it('distinguishes pending local changes and files from acknowledged cloud durability', async () => {
	status.hub = { linked: true, remote_durability: 'pending', backlog: 3, files_pending_backup: 2 };
	render(DevicesSection);
	expect(await screen.findByText('Saved on this worker · cloud backup pending')).toBeTruthy();
	expect(screen.getByText(/3 changes and 2 files/)).toBeTruthy();
	expect(screen.queryByText('Cloud backup up to date')).toBeNull();
	status = { ...status, hub: { linked: true, remote_durability: 'committed', backlog: 0, files_pending_backup: 0 } };
	resourceSync.invalidate(['network']);
	expect(await screen.findByText('Cloud backup up to date')).toBeTruthy();
});
function row(key: string): HTMLElement {
	const el = document.querySelector(`[data-device-key="${key}"]`);
	expect(el).not.toBeNull();
	return el as HTMLElement;
}

it('lists workers, phones and browsers in one device list with no advanced settings', async () => {
	render(DevicesSection);
	expect(await screen.findByText('4 devices')).toBeTruthy();
	expect(screen.getByText('Ready')).toBeTruthy();
	const devices = screen.getByRole('list', { name: 'Devices' });
	expect(within(devices).getAllByRole('listitem')).toHaveLength(4);
	expect(screen.getAllByRole('list')).toHaveLength(1);
	expect(within(row('self')).getByText('This worker')).toBeTruthy();
	expect(within(row('member:laptop')).getByText('Online')).toBeTruthy();
	expect(row('client:phone').textContent).toContain('Phone');
	expect(row('client:browser').textContent).toContain('Browser');
	expect(row('client:browser').querySelector('svg.lucide-globe')).not.toBeNull();
	expect(screen.queryByText(/Pending approvals/)).toBeNull();
	expect(screen.queryByRole('button', { name: /Approve/ })).toBeNull();
	expect(screen.queryByRole('button', { name: /rotate/i })).toBeNull();
	expect(screen.queryByText(/key v1|generation/)).toBeNull();
	expect(screen.queryByText(/fingerprint/i)).toBeNull();
	expect(screen.queryByText('Advanced')).toBeNull();
	expect(screen.queryByRole('button', { name: 'Create account' })).toBeNull();
	expect(api.get.mock.calls.some(([path]) => path.includes('/secret'))).toBe(false);
});

it('renames this worker and removes another worker through the network API', async () => {
	api.patch.mockResolvedValue({});
	api.delete.mockResolvedValue({ device_id: 'laptop', generation: 2, key_version: 2, rotate_provider_keys_recommended: true });
	render(DevicesSection);
	await screen.findByText('4 devices');
	await fireEvent.click(within(row('self')).getByRole('button', { name: 'Rename Desk' }));
	await fireEvent.input(screen.getByRole('textbox', { name: 'Device name' }), { target: { value: 'Studio desk' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Save name' }));
	expect(api.patch).toHaveBeenCalledWith('/network/members/self', { name: 'Studio desk' });
	await fireEvent.click(within(row('member:laptop')).getByRole('button', { name: 'Revoke Laptop' }));
	expect(api.delete).not.toHaveBeenCalled();
	await fireEvent.click(within(row('member:laptop')).getByRole('button', { name: 'Confirm revoke' }));
	expect(api.delete).toHaveBeenCalledWith('/network/members/laptop');
	expect((await screen.findByRole('alert')).textContent).toContain('Laptop');
	expect(screen.getByRole('alert').textContent).toContain('rotate those keys');
});

it('shows devices that are joining right now instead of an approval queue', async () => {
	status = { ...enrolled(), pending_joins: [{ id: 'j1', status: 'pending', device_id: 'dev_new', name: 'New laptop', platform: 'windows', fingerprint, legacy_instance_id: null, created_at: null, expires_at: null }], hub: { linked: true, registered: true, status: 'active', pending_devices: [{ device_id: 'dev_phone', name: 'Office PC', platform: 'windows', signing_public: 'x' }] } };
	render(DevicesSection);
	await screen.findByText('4 devices');
	expect(row('join:j1').textContent).toContain('Joining with an invite');
	expect(row('hub:dev_phone').textContent).toContain('Joining through your account');
	expect(screen.queryByRole('checkbox')).toBeNull();
	expect(screen.queryByRole('button', { name: /Approve|Reject/ })).toBeNull();
});

it('explains waiting on the account and offers the recovery key without any approval step', async () => {
	status = { device: { device_id: 'self', fingerprint }, enrolled: false, platform: 'linux', version: '1.0', instance_id: 'self', enrollment: null, hub: { linked: true, registered: false, status: 'pending', account_network: { network_id: 'n', name: null, founder_device_id: 'other', devices: [{ device_id: 'other', name: 'Office PC', platform: 'windows', kind: 'worker', status: 'active' }], recovery_available: true } } };
	installs = [];
	api.post.mockResolvedValue({ restored: true, network: { name: 'Home' } });
	render(DevicesSection);
	expect((await screen.findByText(/already syncs settings between/)).textContent).toContain('Office PC');
	expect(screen.getByText('Joining')).toBeTruthy();
	expect(screen.queryByText(/fingerprint/i)).toBeNull();
	await fireEvent.input(screen.getByLabelText('Recovery key'), { target: { value: 'A1-AAAAA-AAAAA-AAAAA-AAAAA-AAAAA-A' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Restore' }));
	expect(api.post).toHaveBeenCalledWith('/network/recovery/unlock', { recovery_key: 'A1-AAAAA-AAAAA-AAAAA-AAAAA-AAAAA-A' });
});

it('shows the automatic switch to the account settings with no choice to keep separate ones', async () => {
	const account_network = { network_id: 'other', name: null, founder_device_id: 'x', devices: [{ device_id: 'x', name: 'Office PC', platform: 'windows', kind: 'worker', status: 'active' }], recovery_available: false };
	status = { ...enrolled(), hub: { linked: true, registered: false, status: 'conflict', account_network } };
	api.post.mockResolvedValue({ adopted: true, status: 'pending' });
	render(DevicesSection);
	expect((await screen.findByText(/Your account already syncs settings between/)).textContent).toContain('Office PC');
	expect(screen.getByText('Joining')).toBeTruthy();
	expect(screen.queryByRole('button', { name: /Keep this worker separate/ })).toBeNull();
	expect(screen.queryByRole('button', { name: /Use my account/ })).toBeNull();
	expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
	// Only a failed switch surfaces a retry, which calls the adopt endpoint.
	status = { ...status, hub: { ...status.hub!, last_error: 'Wait for the current settings sync to finish before switching networks' } };
	resourceSync.invalidate(['network']);
	await fireEvent.click(await screen.findByRole('button', { name: 'Retry' }));
	expect(api.post).toHaveBeenCalledWith('/network/hub/adopt', {});
});

it('lets a worker without a network join with an invite or keep its own settings', async () => {
	status = { device: { device_id: 'self', fingerprint }, enrolled: false, platform: 'linux', version: '1.0', instance_id: 'self', enrollment: null };
	installs = [install('self', { is_self: true, device_name: 'Desk' })];
	api.post.mockResolvedValue({ network: { name: null } });
	render(DevicesSection);
	expect(await screen.findByText('Set up this worker')).toBeTruthy();
	expect(screen.getByLabelText('Invite link or code')).toBeTruthy();
	await fireEvent.click(screen.getByRole('button', { name: 'Keep separate settings' }));
	expect(api.post).toHaveBeenCalledWith('/network/create', {});
});

it('confirms client revocation through the client API', async () => {
	render(DevicesSection);
	await screen.findByText('4 devices');
	await fireEvent.click(within(row('client:phone')).getByRole('button', { name: 'Revoke My phone' }));
	expect(api.delete).not.toHaveBeenCalled();
	await fireEvent.click(within(row('client:phone')).getByRole('button', { name: 'Confirm revoke' }));
	expect(api.delete).toHaveBeenCalledWith('/pairing/clients/phone');
	expect(document.querySelector('[data-device-key="client:phone"]')).toBeNull();
	expect(screen.getByText('3 devices')).toBeTruthy();
	expect(appData.dropDeviceSessions).not.toHaveBeenCalled();
});

it('keeps legacy shared-database workers with allow, revoke, rename and remove', async () => {
	installs.push(install('pending', { device_name: 'Old runner', approved: false }));
	api.put.mockImplementation(async (_path: string, body: { approved: boolean }) => install('pending', { device_name: 'Old runner', approved: body.approved }));
	api.patch.mockResolvedValue(install('pending', { device_name: 'Renamed runner' }));
	render(DevicesSection);
	await screen.findByText('5 devices');
	expect(within(row('legacy:pending')).getByText('Shared database')).toBeTruthy();
	await fireEvent.click(within(row('legacy:pending')).getByRole('button', { name: 'Allow Old runner' }));
	expect(api.put).toHaveBeenCalledWith('/cluster/installs/pending/approval', { approved: true });
	await fireEvent.click(await within(row('legacy:pending')).findByRole('button', { name: 'Rename Old runner' }));
	await fireEvent.input(screen.getByRole('textbox', { name: 'Device name' }), { target: { value: 'Renamed runner' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Save name' }));
	expect(api.patch).toHaveBeenCalledWith('/cluster/installs/pending', { device_name: 'Renamed runner' });
	await fireEvent.click(within(row('legacy:pending')).getByRole('button', { name: 'Remove Renamed runner' }));
	await fireEvent.click(within(row('legacy:pending')).getByRole('button', { name: 'Confirm remove' }));
	expect(api.delete).toHaveBeenCalledWith('/cluster/installs/pending');
	expect(appData.dropDeviceSessions).toHaveBeenCalledWith('pending');
});

it.each(['/cluster/installs', '/pairing/clients'])('keeps healthy devices when %s fails and offers a scoped retry', async (failedPath) => {
	api.get.mockImplementation(async (path: string) => { if (path === failedPath) throw new Error('Source unavailable'); return reads(path); });
	render(DevicesSection);
	expect(await screen.findByText('Source unavailable', { exact: false })).toBeTruthy();
	if (failedPath !== '/pairing/clients') expect(screen.getByText('My phone')).toBeTruthy();
	expect(screen.getByText('Desk')).toBeTruthy();
	api.get.mockImplementation(async (path: string) => reads(path));
	await fireEvent.click(screen.getByRole('button', { name: failedPath === '/cluster/installs' ? 'Retry workers' : 'Retry phones and browsers' }));
	await waitFor(() => expect(screen.queryByText('Source unavailable', { exact: false })).toBeNull());
	expect(screen.getByText('4 devices')).toBeTruthy();
});

it('opens Add device with the worker invite first and keeps a created code while closed', async () => {
	vi.useFakeTimers();
	api.post.mockResolvedValue({ grant: { id: 'offer', purpose: 'network:join', expires_at: new Date(Date.now() + 300_000).toISOString() }, code: 'TESTONLYCODE', display_code: 'TEST-ONLY-CODE', pairing_url: null, qr_svg: null, join: { url: 'shuffle://network/join?x', endpoint: 'http://10.0.0.5:8000', code: 'TEST-ONLY-CODE', fingerprint, device_name: 'Desk' } });
	render(DevicesSection);
	await tick(); await vi.advanceTimersByTimeAsync(0);
	await fireEvent.click(screen.getByRole('button', { name: 'Add device' }));
	await tick(); await vi.advanceTimersByTimeAsync(0);
	expect(screen.getByRole('tab', { name: 'Worker' }).getAttribute('aria-selected')).toBe('true');
	await fireEvent.click(screen.getByRole('button', { name: 'Create worker invite' }));
	expect(screen.getByRole('button', { name: 'Cancel code' })).toBeTruthy();
	await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
	expect(screen.queryByRole('button', { name: 'Cancel code' })).toBeNull();
	clients.push(client('late', { device_name: 'Newly paired phone' }));
	await vi.advanceTimersByTimeAsync(10_000);
	expect(screen.getByText('Newly paired phone')).toBeTruthy();
	await fireEvent.click(screen.getByRole('button', { name: 'Add device' }));
	expect(screen.getByRole('button', { name: 'Cancel code' })).toBeTruthy();
	expect(api.post).toHaveBeenCalledTimes(1);
});

it('refreshes on network events without losing a rename draft and stops polling when unmounted', async () => {
	vi.useFakeTimers();
	const view = render(DevicesSection);
	await tick(); await vi.advanceTimersByTimeAsync(0);
	await fireEvent.click(within(row('member:laptop')).getByRole('button', { name: 'Rename Laptop' }));
	await fireEvent.input(screen.getByRole('textbox', { name: 'Device name' }), { target: { value: 'Unsaved name' } });
	status = { ...enrolled(), members: [...enrolled().members!, member('tablet', { name: 'Tablet' })] };
	api.get.mockClear();
	resourceSync.invalidate(['network']);
	await vi.advanceTimersByTimeAsync(100);
	expect(screen.getByText('Tablet')).toBeTruthy();
	expect((screen.getByRole('textbox', { name: 'Device name' }) as HTMLInputElement).value).toBe('Unsaved name');
	expect(api.get.mock.calls.some(([path]) => path === '/network/status')).toBe(true);
	view.unmount();
	const count = api.get.mock.calls.length;
	await vi.advanceTimersByTimeAsync(20_000);
	expect(api.get).toHaveBeenCalledTimes(count);
});

it('does not expose advanced network settings or load their data', async () => {
	render(DevicesSection);
	await screen.findByText('4 devices');
	expect(screen.queryByText('Advanced')).toBeNull();
	expect(screen.queryByLabelText("This worker's address")).toBeNull();
	expect(screen.queryByRole('button', { name: 'Create recovery key' })).toBeNull();
	expect(screen.queryByText("Join a different worker's network")).toBeNull();
	expect(api.get.mock.calls.some(([path]) => ['/cluster/advertise', '/network/conflicts'].includes(path))).toBe(false);
});


it.each(['servers', 'devices'] as const)('confirms account %s revocation, refreshes inventory and retains failures', async (kind) => {
	const id = 'offline';
	let profile = {
		account: { id: 'account', username: 'owner', profile_complete: true },
		servers: [{ id, name: 'Offline PC', current: false, url: null }],
		devices: [{ id, name: 'Offline phone', platform: 'android' }]
	};
	api.get.mockImplementation(async (path: string) => {
		if (path === '/mobile/hub/connection') return { ...disconnected, signed_in: true, status: 'connected' };
		if (path === '/mobile/hub/account') return structuredClone(profile);
		return reads(path);
	});
	render(DevicesSection);
	const name = kind === 'servers' ? 'Offline PC' : 'Offline phone';
	const button = await screen.findByRole('button', { name: `Revoke ${name} from account` });
	await fireEvent.click(button);
	expect(api.delete).not.toHaveBeenCalled();
	await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
	expect(api.delete).not.toHaveBeenCalled();
	await fireEvent.click(screen.getByRole('button', { name: `Revoke ${name} from account` }));
	api.delete.mockRejectedValueOnce(new Error('Account service unavailable'));
	await fireEvent.click(screen.getByRole('button', { name: 'Confirm revoke' }));
	expect(await screen.findByText('Account service unavailable')).toBeTruthy();
	expect(screen.getByText(name)).toBeTruthy();
	let finish!: (value: unknown) => void;
	api.delete.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
	await fireEvent.click(screen.getByRole('button', { name: 'Confirm revoke' }));
	expect(screen.getByRole('button', { name: 'Revoking…' }).hasAttribute('disabled')).toBe(true);
	profile = { ...profile, [kind]: [] };
	finish({ ok: true });
	await waitFor(() => expect(screen.queryByText(name)).toBeNull());
	expect(api.delete).toHaveBeenLastCalledWith(`/mobile/hub/account/${kind}/${id}`);
	expect(api.get.mock.calls.filter(([path]) => path === '/mobile/hub/account').length).toBeGreaterThan(1);
	expect(screen.getByText('My phone')).toBeTruthy();
});

it('includes offline account devices in the same list, merges known local identities, and refreshes registrations', async () => {
	vi.useFakeTimers();
	status.members![1].endpoints = ['https://laptop.example/'];
	let profile = {
		account: { id: 'account', username: 'owner', profile_complete: true },
		servers: [
			{ id: 'server-self', name: 'Desk account name', current: true, url: null },
			{ id: 'server-laptop', name: 'Laptop account name', current: false, url: 'https://laptop.example' },
			{ id: 'offline', name: 'Offline PC', current: false, url: null }
		],
		devices: [
			{ id: 'account-phone', local_device_id: 'phone', name: 'Phone account name', platform: 'android' },
			{ id: 'browser', name: 'Browser account name', platform: 'web' },
			{ id: 'offline-phone', name: 'Offline phone', platform: 'ios' },
			{ id: 'same-name', name: 'Laptop', platform: 'web' }
		]
	};
	api.get.mockImplementation(async (path: string) => {
		if (path === '/mobile/hub/connection') return { ...disconnected, signed_in: true, status: 'connected' };
		if (path === '/mobile/hub/account') return profile;
		return reads(path);
	});
	render(DevicesSection);
	await tick(); await vi.advanceTimersByTimeAsync(0);
	expect(screen.getByText('7 devices')).toBeTruthy();
	expect(screen.getAllByRole('list')).toHaveLength(1);
	expect(within(screen.getByRole('list', { name: 'Devices' })).getAllByRole('listitem')).toHaveLength(7);
	expect(row('account-worker:offline').textContent).toContain('Offline PC');
	expect(row('account-device:offline-phone').textContent).toContain('Phone');
	expect(row('account-device:same-name').textContent).toContain('Browser');
	expect(screen.queryByText('Desk account name')).toBeNull();
	expect(screen.queryByText('Laptop account name')).toBeNull();
	expect(screen.queryByText('Phone account name')).toBeNull();
	expect(screen.queryByText('Browser account name')).toBeNull();
	expect(within(row('member:laptop')).getByRole('button', { name: 'Revoke Laptop' })).toBeTruthy();
	expect(within(row('client:phone')).getByRole('button', { name: 'Revoke My phone' })).toBeTruthy();
	expect(within(row('client:browser')).getByRole('button', { name: 'Revoke Work browser' })).toBeTruthy();
	expect(screen.queryByRole('button', { name: 'Revoke Desk account name from account' })).toBeNull();
	profile = { ...profile, devices: [] };
	await vi.advanceTimersByTimeAsync(32_000);
	expect(screen.queryByText('Offline phone')).toBeNull();
	expect(screen.getByText('5 devices')).toBeTruthy();
	expect(row('client:phone')).toBeTruthy();
});

it.each(['phone', 'member', 'legacy'] as const)('uses one icon to revoke both account and local %s access', async (kind) => {
	status.members![1].endpoints = ['https://laptop.example/'];
	if (kind === 'legacy') installs.push(install('old', { device_name: 'Old worker', base_url: 'https://old.example' }));
	const isPhone = kind === 'phone';
	let profile = {
		account: { id: 'account', username: 'owner', profile_complete: true },
		servers: isPhone ? [] : [{ id: 'linked', name: 'Account worker', current: false, url: kind === 'member' ? 'https://laptop.example' : 'https://old.example' }],
		devices: isPhone ? [{ id: 'linked', local_device_id: 'phone', name: 'Account phone', platform: 'android' }] : []
	};
	api.get.mockImplementation(async (path: string) => {
		if (path === '/mobile/hub/connection') return { ...disconnected, signed_in: true, status: 'connected' };
		if (path === '/mobile/hub/account') return structuredClone(profile);
		return reads(path);
	});
	const events: string[] = [];
	api.delete.mockImplementation(async (path: string) => {
		events.push(path);
		if (path.startsWith('/mobile/hub/account/')) profile = { ...profile, servers: [], devices: [] };
		if (path.startsWith('/network/members/')) status.members = status.members!.filter((item) => item.device_id !== 'laptop');
		return { ok: true, rotate_provider_keys_recommended: true };
	});
	api.put.mockImplementation(async (path: string) => { events.push(path); return install('old', { approved: false }); });
	render(DevicesSection);
	await screen.findByText('@owner');
	const key = isPhone ? 'client:phone' : kind === 'member' ? 'member:laptop' : 'legacy:old';
	const actions = within(row(key)).getAllByRole('button', { name: /^Revoke / });
	expect(actions).toHaveLength(1);
	expect(actions[0].classList.contains('icon')).toBe(true);
	expect(actions[0].textContent).toBe('');
	expect(screen.queryByText('Revoke from account')).toBeNull();
	await fireEvent.click(actions[0]);
	expect(within(row(key)).getByText(/also revokes its account access/)).toBeTruthy();
	await fireEvent.click(within(row(key)).getByRole('button', { name: 'Cancel' }));
	expect(events).toEqual([]);
	await fireEvent.click(within(row(key)).getByRole('button', { name: /^Revoke / }));
	await fireEvent.click(within(row(key)).getByRole('button', { name: 'Confirm revoke' }));
	await waitFor(() => expect(events).toEqual([
		`/mobile/hub/account/${isPhone ? 'devices' : 'servers'}/linked`,
		isPhone ? '/pairing/clients/phone' : kind === 'member' ? '/network/members/laptop' : '/cluster/installs/old/approval'
	]));
});

it('stops on account failure and lets local cleanup retry after account revocation succeeds', async () => {
	let profile = { account: { id: 'account', username: 'owner', profile_complete: true }, servers: [],
		devices: [{ id: 'linked', local_device_id: 'phone', name: 'Account phone', platform: 'android' }] };
	api.get.mockImplementation(async (path: string) => {
		if (path === '/mobile/hub/connection') return { ...disconnected, signed_in: true, status: 'connected' };
		if (path === '/mobile/hub/account') return structuredClone(profile);
		return reads(path);
	});
	render(DevicesSection);
	await screen.findByText('@owner');
	await fireEvent.click(within(row('client:phone')).getByRole('button', { name: 'Revoke My phone' }));
	api.delete.mockRejectedValueOnce(new Error('Account service unavailable'));
	await fireEvent.click(screen.getByRole('button', { name: 'Confirm revoke' }));
	expect(await screen.findByText('Account service unavailable')).toBeTruthy();
	expect(api.delete.mock.calls).toEqual([['/mobile/hub/account/devices/linked']]);
	api.delete.mockImplementationOnce(async () => { profile = { ...profile, devices: [] }; return { ok: true }; });
	api.delete.mockRejectedValueOnce(new Error('Local cleanup failed'));
	await fireEvent.click(screen.getByRole('button', { name: 'Confirm revoke' }));
	expect(await screen.findByText(/Account access was revoked. Retry/)).toBeTruthy();
	api.delete.mockResolvedValue({ ok: true });
	await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm revoke' }).hasAttribute('disabled')).toBe(false));
	await fireEvent.click(screen.getByRole('button', { name: 'Confirm revoke' }));
	await waitFor(() => expect(document.querySelector('[data-device-key="client:phone"]')).toBeNull());
	expect(api.delete.mock.calls.filter(([path]) => path === '/mobile/hub/account/devices/linked')).toHaveLength(2);
	expect(api.delete.mock.calls.filter(([path]) => path === '/pairing/clients/phone')).toHaveLength(2);
});
