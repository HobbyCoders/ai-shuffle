/**
 * Request-shape tests for the worker-network API client.
 *
 * The `api` client is mocked: each function must hit the documented path with
 * the documented verb and body. Admission calls carry an empty body (the
 * one-time code or the account is the authorization; nothing is compared)
 * and the join grant MUST be minted with `purpose: 'network:join'`.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

const api = vi.hoisted(() => ({
	get: vi.fn(),
	post: vi.fn(),
	put: vi.fn(),
	patch: vi.fn(),
	delete: vi.fn()
}));
vi.mock('./client', () => ({ api }));

import {
	approveHubDevice,
	approveJoin,
	cancelEnrollment,
	createNetwork,
	createNetworkJoinGrant,
	createRecoveryKey,
	dismissNetworkConflict,
	forkSessionHere,
	getEnrollment,
	getLanSettings,
	getNetworkPolicy,
	getNetworkStatus,
	getRecoveryStatus,
	listHubPendingDevices,
	listNetworkConflicts,
	listNetworkMembers,
	listNetworkRunners,
	listPendingJoins,
	rejectHubDevice,
	rejectJoin,
	renameNetworkMember,
	revokeNetworkMember,
	routeLabel,
	startEnrollment,
	updateLanSettings
} from './network';

beforeEach(() => {
	vi.resetAllMocks();
});

describe('status / members', () => {
	it('GETs the status without caching', async () => {
		api.get.mockResolvedValue({ enrolled: false });
		await getNetworkStatus();
		expect(api.get).toHaveBeenCalledWith('/network/status', { cache: 'no-store' });
	});

	it('POSTs create with only the fields the user filled in', async () => {
		api.post.mockResolvedValue({ network: { network_id: 'n1' } });
		await createNetwork({ name: '  Home  ', device_name: '' });
		expect(api.post).toHaveBeenCalledWith('/network/create', { name: 'Home' });
		await createNetwork();
		expect(api.post).toHaveBeenLastCalledWith('/network/create', {});
	});

	it('unwraps the members list', async () => {
		api.get.mockResolvedValue({ members: [{ device_id: 'd1' }] });
		expect(await listNetworkMembers()).toEqual([{ device_id: 'd1' }]);
		expect(api.get).toHaveBeenCalledWith('/network/members');
	});

	it('PATCHes a rename with a trimmed name and an encoded id', async () => {
		api.patch.mockResolvedValue({ device_id: 'd/1', name: 'Laptop' });
		await renameNetworkMember('d/1', ' Laptop ');
		expect(api.patch).toHaveBeenCalledWith('/network/members/d%2F1', { name: 'Laptop' });
	});

	it('DELETEs a member and passes the rotation advice through', async () => {
		api.delete.mockResolvedValue({ device_id: 'd2', generation: 3, key_version: 2, rotate_provider_keys_recommended: true });
		const result = await revokeNetworkMember('d2');
		expect(api.delete).toHaveBeenCalledWith('/network/members/d2');
		expect(result.rotate_provider_keys_recommended).toBe(true);
	});
});

describe('conflicts / runners / policy', () => {
	it('lists and dismisses conflicts', async () => {
		api.get.mockResolvedValue({ conflicts: [{ id: 7 }] });
		expect(await listNetworkConflicts()).toEqual([{ id: 7 }]);
		expect(api.get).toHaveBeenCalledWith('/network/conflicts');
		api.delete.mockResolvedValue(undefined);
		await dismissNetworkConflict(7);
		expect(api.delete).toHaveBeenCalledWith('/network/conflicts/7');
	});

	it('GETs the runner directory and the policy', async () => {
		api.get.mockResolvedValue({ runners: [] });
		await listNetworkRunners();
		expect(api.get).toHaveBeenCalledWith('/network/runners');
		await getNetworkPolicy();
		expect(api.get).toHaveBeenLastCalledWith('/network/policy');
	});
});

describe('recovery key', () => {
	it('GETs the configured state', async () => {
		api.get.mockResolvedValue({ configured: false });
		await getRecoveryStatus();
		expect(api.get).toHaveBeenCalledWith('/network/recovery');
	});

	it('POSTs to create and returns the one-time key untouched', async () => {
		api.post.mockResolvedValue({ recovery_key: 'A1-AAAAA-BBBBB-CCCCC-DDDDD-EEEEE-F', created_at: 'x', key_version: 1 });
		const created = await createRecoveryKey();
		expect(api.post).toHaveBeenCalledWith('/network/recovery', {});
		expect(created.recovery_key).toBe('A1-AAAAA-BBBBB-CCCCC-DDDDD-EEEEE-F');
	});

});

describe('inviting (this worker is the inviter)', () => {
	it('mints a network:join pairing grant bound to the chosen endpoint', async () => {
		api.post.mockResolvedValue({ code: 'x', join: null });
		await createNetworkJoinGrant('https://10.0.0.5:8000');
		expect(api.post).toHaveBeenCalledWith('/pairing/grants', { purpose: 'network:join', endpoint_url: 'https://10.0.0.5:8000' });
	});

	it('lists pending joins', async () => {
		api.get.mockResolvedValue({ joins: [{ id: 'j1' }] });
		expect(await listPendingJoins()).toEqual([{ id: 'j1' }]);
		expect(api.get).toHaveBeenCalledWith('/network/joins');
	});

	it('admits a parked join and rejects, both with an empty body', async () => {
		api.post.mockResolvedValue({ status: 'approved' });
		await approveJoin('j 1');
		expect(api.post).toHaveBeenCalledWith('/network/joins/j%201/approve', {});
		await rejectJoin('j1');
		expect(api.post).toHaveBeenLastCalledWith('/network/joins/j1/reject', {});
	});
});

describe('enrolling (this worker joins)', () => {
	it('POSTs a pasted link as { link }', async () => {
		api.post.mockResolvedValue({ join_id: 'j', status: 'pending' });
		await startEnrollment({ link: ' shuffle://network/join?endpoint=x&code=y&fp=z ' });
		expect(api.post).toHaveBeenCalledWith('/network/enroll', { link: 'shuffle://network/join?endpoint=x&code=y&fp=z' });
	});

	it('POSTs the manual triple plus the optional device name', async () => {
		api.post.mockResolvedValue({ join_id: 'j', status: 'pending' });
		await startEnrollment({ endpoint_url: 'https://a:8000 ', code: 'ABCD-EFGH-JKLM', inviter_fingerprint: '1111-2222', device_name: 'Mini' });
		expect(api.post).toHaveBeenCalledWith('/network/enroll', {
			endpoint_url: 'https://a:8000',
			code: 'ABCD-EFGH-JKLM',
			inviter_fingerprint: '1111-2222',
			device_name: 'Mini'
		});
	});

	it('polls and cancels the enrollment', async () => {
		api.get.mockResolvedValue({ enrollment: null, enrolled: false });
		await getEnrollment();
		expect(api.get).toHaveBeenCalledWith('/network/enroll', { cache: 'no-store' });
		api.delete.mockResolvedValue(undefined);
		await cancelEnrollment();
		expect(api.delete).toHaveBeenCalledWith('/network/enroll');
	});
});

describe('account (hub) device approvals', () => {
	it('lists the pending devices', async () => {
		api.get.mockResolvedValue({ devices: [{ device_id: 'h1' }] });
		expect(await listHubPendingDevices()).toEqual([{ device_id: 'h1' }]);
		expect(api.get).toHaveBeenCalledWith('/network/hub/devices');
	});

	it('admits an account device and rejects, both with an empty body', async () => {
		api.post.mockResolvedValue({ status: 'approved' });
		await approveHubDevice('h1');
		expect(api.post).toHaveBeenCalledWith('/network/hub/devices/h1/approve', {});
		await rejectHubDevice('h1');
		expect(api.post).toHaveBeenLastCalledWith('/network/hub/devices/h1/reject', {});
	});
});

describe('fork here', () => {
	it('POSTs the fork-here route for the session', async () => {
		api.post.mockResolvedValue({ session_id: 'new', parent_session_id: 'old', context_chars: 10 });
		const result = await forkSessionHere('old');
		expect(api.post).toHaveBeenCalledWith('/network/sessions/old/fork-here', {});
		expect(result.session_id).toBe('new');
	});
});

describe('LAN-direct sync', () => {
	it('reads the setting uncached and saves only what changed', async () => {
		api.get.mockResolvedValue({ enabled: true, addresses: [], advertised: [] });
		await getLanSettings();
		expect(api.get).toHaveBeenCalledWith('/network/lan', { cache: 'no-store' });
		api.put.mockResolvedValue({ enabled: false, addresses: [], advertised: [] });
		await updateLanSettings({ enabled: false });
		expect(api.put).toHaveBeenCalledWith('/network/lan', { enabled: false });
		await updateLanSettings({ addresses: ['10.0.0.20:8001'] });
		expect(api.put).toHaveBeenLastCalledWith('/network/lan', { addresses: ['10.0.0.20:8001'] });
	});

	it('labels each peer route', () => {
		expect(routeLabel('lan')).toBe('Direct (LAN)');
		expect(routeLabel('tunnel')).toBe('Via tunnel');
		expect(routeLabel('direct')).toBe('Direct');
		expect(routeLabel(null)).toBe('Not connected');
	});
});
