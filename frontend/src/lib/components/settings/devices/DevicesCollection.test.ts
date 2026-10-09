/**
 * Devices collection: the grouped list, the approval confirm ("IDs match?"),
 * revoke with the rotate-keys toast, Add device, the Network tab, and
 * registry coverage.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import type { Install } from '$lib/api/installs';
import type { PairedClient } from '$lib/api/pairing';
import type { NetworkMember, NetworkStatus } from '$lib/api/network';

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() }));
const banner = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', () => ({ api, getErrorMessage: (e: Error) => e.message }));
vi.mock('$lib/stores/app-data.svelte', () => ({ appData: { dropDeviceSessions: vi.fn() } }));
vi.mock('$lib/stores/notifications', () => ({ notify: { banner } }));
vi.mock('$lib/stores/auth.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { isAdmin: writable(true), auth: { current: { status: { auth_mode: 'local' } }, logout: vi.fn() } };
});

import DevicesCollection from './DevicesCollection.svelte';
import { DevicesModel } from './devices-model.svelte';
import { settingsNav } from '$lib/settings/navigation.svelte';
import { assertCategoryCoverage } from '$lib/settings/testing/coverage';
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
	hub: { linked: false }
});
const request = {
	device_id: 'dev-a',
	hub_device_id: 'dv_7f3a91c2e4',
	paired_client_id: 'pc-a',
	mobile_record_id: 'k-a',
	device_name: 'Pixel 9 Pro',
	platform: 'android',
	confirmation: 'pending',
	revoked_at: null,
	api_key_expires_at: '2099-01-01T00:00:00Z'
};
const profile = {
	account: { id: 'a', username: 'owner', profile_complete: true },
	servers: [{ id: 's', name: 'Desk', current: true, url: null }],
	devices: []
};
let status: NetworkStatus;
let connection: unknown;
function reads(path: string): unknown {
	if (path === '/network/status') return status;
	if (path === '/cluster/installs')
		return {
			installs: [install('self', { device_id: 'self', is_self: true, device_name: 'Desk' })],
			self_instance_id: 'self'
		};
	if (path === '/pairing/clients') return { clients: [client('phone')] };
	if (path === '/pairing/endpoints')
		return {
			endpoints: [
				{
					url: 'http://10.0.0.5:8000',
					kind: 'lan',
					label: 'Home LAN',
					secure: false,
					loopback: false,
					qr_eligible: true,
					source: 'advertised'
				}
			]
		};
	if (path === '/network/lan') return { enabled: true, addresses: [], advertised: ['https://desk.example.com', 'http://10.0.0.5:8000'] };
	if (path === '/mobile/hub/connection') return connection;
	if (path === '/mobile/hub/account') return structuredClone(profile);
	if (path === '/mobile/hub/devices/confirmations') return [{ ...request }];
	throw new Error('Unexpected GET ' + path);
}

let model: DevicesModel;
let keepAlive: () => void;

beforeEach(async () => {
	vi.resetAllMocks();
	settingsNav.reset();
	status = enrolled();
	connection = { signed_in: true, status: 'connected', code: null, qr_svg: null, error: null };
	api.get.mockImplementation(async (path: string) => reads(path));
	model = new DevicesModel();
	keepAlive = model.start();
	await vi.waitFor(() => expect(model.complete && model.requests.loaded).toBe(true));
});
afterEach(() => {
	cleanup();
	keepAlive();
	model.reset();
	resourceSync.clear();
});

function show(item?: string, tab?: string) {
	settingsNav.go({ category: 'devices', ...(item ? { item } : {}), ...(tab ? { tab } : {}) });
	return render(DevicesCollection, { props: { model } });
}

describe('Devices list', () => {
	it('shows a desktop revision beside its bare version in the list and detail', async () => {
		status.members![1] = member('laptop', { platform: 'windows', version: '0.9.1', revision: '04d9a0cfb' });
		await model.loadStatus();
		show('member:laptop');
		expect(screen.getByRole('button', { name: /Laptop.*04d9a0cfb/ }).querySelector('.tx > .sm')?.textContent).toBe('04d9a0cfb, 0.9.1, windows, online');
		const revision = screen.getByText('Revision');
		expect(revision.nextElementSibling?.textContent).toBe('04d9a0cfb');
		expect(screen.getByText('0.9.1')).toBeTruthy();
	});

	it('shows the local revision even before joining a network', async () => {
		status = { device: { device_id: 'self', fingerprint }, enrolled: false, platform: 'linux',
			version: '0.9.1', revision: '123456789', instance_id: 'self' };
		await model.loadStatus();
		show('self');
		expect(screen.getByRole('button', { name: /123456789/ })).toBeTruthy();
		expect(screen.getAllByText('123456789').length).toBe(2);
	});

	it('labels an older peer revision Unknown rather than inventing one', () => {
		show('member:laptop');
		const revision = screen.getByText('Revision');
		expect(revision.nextElementSibling?.textContent).toBe('Unknown');
	});

	it('groups the account, requests, this worker, workers and phones', () => {
		show();
		for (const label of ['Account', 'Requests', 'This worker', 'Workers', 'Phones and browsers']) {
			expect(screen.getByText(label)).toBeTruthy();
		}
		expect(screen.getByRole('button', { name: /Shuffle account/ }).textContent).toContain('@owner, remote access on');
		expect(screen.getByRole('button', { name: /Pixel 9 Pro/ }).textContent).toContain('Wants to be approved');
		expect(screen.getByRole('button', { name: /Desk/ }).textContent).toContain('1.0, linux, ready');
		expect(screen.queryByText(/admin/i)).toBeNull();
	});

	it('approves a request only after "IDs match?"', async () => {
		api.post.mockResolvedValue({ confirmation: 'approved' });
		show('req:dev-a:pc-a');
		expect(screen.getAllByText('dv_7f3a91c2e4').length).toBeGreaterThan(0);
		await fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
		expect(api.post).not.toHaveBeenCalled();
		expect(screen.getByText('IDs match?')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
		await vi.waitFor(() =>
			expect(api.post).toHaveBeenCalledWith('/mobile/hub/devices/dev-a/confirmation', {
				paired_client_id: 'pc-a',
				mobile_record_id: 'k-a',
				approved: true
			})
		);
	});

	it('denies a request from the danger zone', async () => {
		api.post.mockResolvedValue({ confirmation: 'denied' });
		show('req:dev-a:pc-a');
		await fireEvent.click(screen.getByRole('button', { name: 'Deny' }));
		expect(screen.getByText('Deny Pixel 9 Pro?')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Deny' }));
		await vi.waitFor(() =>
			expect(api.post).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ approved: false }))
		);
	});

	it('revokes a worker and offers to rotate provider keys', async () => {
		api.delete.mockResolvedValue({
			device_id: 'laptop',
			generation: 2,
			key_version: 2,
			rotate_provider_keys_recommended: true
		});
		show('member:laptop');
		await fireEvent.click(screen.getByRole('button', { name: 'Revoke' }));
		expect(api.delete).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Revoke' }));
		await vi.waitFor(() => expect(api.delete).toHaveBeenCalledWith('/network/members/laptop'));
		expect(banner.mock.calls[0][1].action.label).toBe('Open providers');
		await vi.waitFor(() => expect(settingsNav.selectedItem('devices')).toBeNull());
	});

	it('Add device makes a pairing code item and shows the code', async () => {
		api.post.mockResolvedValue({
			grant: {
				id: 'g',
				purpose: 'network:join',
				expires_at: new Date(Date.now() + 600_000).toISOString(),
				endpoint_url: 'http://10.0.0.5:8000'
			},
			code: 'X',
			display_code: 'K3X9-P2MQ',
			pairing_url: null,
			qr_svg: '<svg></svg>',
			join: {
				url: 'shuffle://network/join?x',
				endpoint: 'http://10.0.0.5:8000',
				code: 'K3X9-P2MQ',
				fingerprint,
				device_name: 'Desk'
			}
		});
		show();
		await fireEvent.click(screen.getByRole('button', { name: 'Add device' }));
		expect(await screen.findByText('K3X9-P2MQ')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Copy invite link' })).toBeTruthy();
		expect(screen.getByLabelText('Worker invite QR code')).toBeTruthy();
		expect(screen.getByText('Pairing codes')).toBeTruthy();
	});

	it.each([false, true])('recovers an unfinished transfer with explicit wake choice %s', async (releaseWakes) => {
		status.handoff_transfers = { pending: [{ token: 'transfer-1', session_id: 'chat-1', title: 'Moved chat',
			source: 'Offline worker', can_abandon: true, reason: 'source_offline' }], history: [] };
		await model.loadStatus();
		api.post.mockImplementation(async () => {
			status.handoff_transfers = { pending: [], history: [{ session_id: 'chat-1', title: 'Moved chat',
				reason: releaseWakes ? 'owner_resumed_wakes' : 'owner_discarded_wakes' }] };
			return { session_id: 'chat-1', released_wakes: Number(releaseWakes) };
		});
		show('self', 'Network');
		expect(screen.getByText('Unfinished chat transfers')).toBeTruthy();
		const checkbox = screen.getByRole('checkbox', { name: 'Resume remaining wake-ups' });
		expect((checkbox as HTMLInputElement).checked).toBe(false);
		if (releaseWakes) await fireEvent.click(checkbox);
		await fireEvent.click(screen.getByRole('button', { name: 'Abandon transfer' }));
		expect(api.post).not.toHaveBeenCalled();
		expect(screen.getByText(releaseWakes ? 'Earlier messages may be missing. Resume remaining wake-ups anyway?' : 'Keep this chat here and discard its paused wake-ups?')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Abandon transfer' }));
		await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith('/network/handoff-transfers/transfer-1/abandon', { release_wakes: releaseWakes }));
		expect(await screen.findByText('Recovered chat transfers')).toBeTruthy();
	});

	it('shows a waiting transfer without an abandon action before the threshold', async () => {
		status.handoff_transfers = { pending: [{ token: 'early', session_id: 'chat', title: 'Waiting chat',
			source: 'Worker', can_abandon: false, reason: 'source_offline' }], history: [] };
		await model.loadStatus();
		show('self', 'Network');
		expect(screen.getByText('Recovery available after five minutes offline')).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Abandon transfer' })).toBeNull();
	});

	it('joins another network only after "Leave Home?"', async () => {
		api.post.mockResolvedValue({ join_id: 'j', status: 'approved', inviter: {}, your_fingerprint: 'x' });
		show('self', 'Network');
		expect(screen.getByText('2 workers, direct sync')).toBeTruthy();
		await fireEvent.input(screen.getByRole('textbox', { name: 'Invite link or code' }), {
			target: { value: 'shuffle://network/join?endpoint=http%3A%2F%2F10.0.0.9%3A8000&code=ABCD-EFGH-JKLM' }
		});
		await fireEvent.click(screen.getByRole('button', { name: 'Join' }));
		expect(api.post).not.toHaveBeenCalled();
		expect(screen.getByText('Leave Home?')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Leave and join' }));
		await vi.waitFor(() =>
			expect(api.post).toHaveBeenCalledWith('/network/enroll', expect.objectContaining({ replace: true }))
		);
	});

	it('keeps separate settings on a worker without a network', async () => {
		status = {
			device: { device_id: 'self', fingerprint },
			enrolled: false,
			platform: 'linux',
			version: '1.0',
			instance_id: 'self',
			enrollment: null
		};
		await model.loadStatus();
		api.post.mockResolvedValue({ network: { name: null } });
		show('self', 'Network');
		await fireEvent.click(screen.getByRole('button', { name: 'Keep separate settings' }));
		expect(api.post).toHaveBeenCalledWith('/network/create', {});
	});

	it('shows how a peer is reached', async () => {
		status.members![1] = member('laptop', { route: 'lan' });
		await model.loadStatus();
		show('member:laptop');
		expect(screen.getByText('Connection').closest('.st-row, [class*=row]')?.textContent).toContain('Direct (LAN)');
	});

	it('turns LAN-direct sync off and saves an extra address', async () => {
		api.put.mockImplementation(async (_path: string, body: { enabled?: boolean; addresses?: string[] }) => ({
			enabled: body.enabled ?? true,
			addresses: body.addresses ?? [],
			advertised: []
		}));
		show('self', 'Network');
		const toggle = await screen.findByRole('switch', { name: /Sync directly on the LAN/ });
		expect(toggle.getAttribute('aria-checked')).toBe('true');
		expect(screen.getByText('http://10.0.0.5:8000')).toBeTruthy();
		await fireEvent.click(toggle);
		expect(api.put).toHaveBeenCalledWith('/network/lan', { enabled: false });
		await vi.waitFor(() => expect(screen.getByRole('switch', { name: /Sync directly on the LAN/ }).getAttribute('aria-checked')).toBe('false'));
	});

	it('shows the tunnel limit refusal with a License link', async () => {
		const message = 'Remote access is off: this account already uses its one remote connection.';
		connection = {
			signed_in: true,
			profile_ready: false,
			status: 'limited',
			code: null,
			qr_svg: null,
			error: message,
			refusal: { code: 'tunnel_limit', limit: 1, used: 1, licensed: false, message }
		};
		await model.account.refresh();
		show('account');
		const alert = screen.getAllByRole('alert').find((a) => a.textContent?.includes('1 of 1 remote connection in use'));
		expect(alert).toBeTruthy();
		expect(screen.getAllByText(new RegExp(message.replace(/[.:]/g, '.'))).length).toBe(1);
		const banner = alert!.closest('.st-banner') as HTMLElement;
		await fireEvent.click(within(banner).getByRole('button', { name: 'Open License' }));
		expect(settingsNav.category).toBe('system');
		expect(settingsNav.pendingAnchor).toBe('system.license');
	});
});

it('renders every Devices registry entry where it lives', async () => {
	await assertCategoryCoverage('devices', ({ item, tab }) => show(item, tab).container);
	for (const tab of ['Overview', 'Network']) {
		show('self', tab);
		expect(document.querySelector('[data-setting-anchor]')).toBeNull();
		cleanup();
	}
});
