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
function row(key: string): HTMLElement {
	const el = document.querySelector(`[data-device-key="${key}"]`);
	expect(el).not.toBeNull();
	return el as HTMLElement;
}

it('lists this worker, network workers and paired devices in two groups with one count and no approval controls', async () => {
	render(DevicesSection);
	expect(await screen.findByText('4 devices')).toBeTruthy();
	expect(screen.getByText('Ready')).toBeTruthy();
	const workers = screen.getByRole('list', { name: 'Workers' });
	expect(within(workers).getAllByRole('listitem')).toHaveLength(2);
	expect(within(row('self')).getByText('This worker')).toBeTruthy();
	expect(within(row('member:laptop')).getByText('Online')).toBeTruthy();
	const phones = screen.getByRole('list', { name: 'Phones and browsers' });
	expect(within(phones).getAllByRole('listitem')).toHaveLength(2);
	expect(row('client:phone').textContent).toContain('Phone');
	expect(row('client:browser').textContent).toContain('Browser');
	expect(row('client:browser').querySelector('svg.lucide-globe')).not.toBeNull();
	expect(screen.queryByText(/Pending approvals/)).toBeNull();
	expect(screen.queryByRole('button', { name: /Approve/ })).toBeNull();
	expect(screen.queryByRole('button', { name: /rotate/i })).toBeNull();
	expect(screen.queryByText(/key v1|generation/)).toBeNull();
	expect(screen.queryByText(/fingerprint/i)).toBeNull();
	expect(screen.getByText('Advanced').closest('details')?.open).toBe(false);
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
	await fireEvent.click(within(row('member:laptop')).getByRole('button', { name: 'Remove Laptop' }));
	expect(api.delete).not.toHaveBeenCalled();
	await fireEvent.click(within(row('member:laptop')).getByRole('button', { name: 'Confirm remove' }));
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

it('offers to switch to the account settings when the account network differs', async () => {
	status = { ...enrolled(), hub: { linked: true, registered: false, status: 'conflict', account_network: { network_id: 'other', name: null, founder_device_id: 'x', devices: [{ device_id: 'x', name: 'Office PC', platform: 'windows', kind: 'worker', status: 'active' }], recovery_available: false } } };
	api.post.mockResolvedValue({ adopted: true, status: 'pending' });
	render(DevicesSection);
	expect((await screen.findByRole('alert')).textContent).toContain('Office PC');
	expect(screen.getByText('Account differs')).toBeTruthy();
	await fireEvent.click(screen.getByRole('button', { name: "Use my account's settings" }));
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

it('creates the recovery key from Advanced and keeps the address controls there', async () => {
	api.post.mockImplementation(async (path: string) => {
		if (path !== '/network/recovery') throw new Error('Unexpected POST ' + path);
		return { recovery_key: 'SYNTHETIC-RECOVERY-KEY', created_at: '2026-09-01T10:00:00Z', key_version: 1 };
	});
	api.put.mockResolvedValue({ ...advertise, override: 'https://new.example.com' });
	render(DevicesSection);
	await screen.findByText('4 devices');
	const details = screen.getByText('Advanced').closest('details')!;
	details.open = true;
	await fireEvent.click(screen.getByRole('button', { name: 'Create recovery key' }));
	expect(await screen.findByText('SYNTHETIC-RECOVERY-KEY')).toBeTruthy();
	expect(api.post).toHaveBeenCalledExactlyOnceWith('/network/recovery', {});
	await fireEvent.input(screen.getByLabelText("This worker's address"), { target: { value: 'https://new.example.com' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Save address' }));
	expect(api.put).toHaveBeenCalledWith('/cluster/advertise', { base_url: 'https://new.example.com' });
});
