/**
 * Tests for the pure network-status helpers: state pill derivation and the
 * client-side fingerprint (which must match `app.network.crypto.fingerprint`).
 */

import { describe, it, expect } from 'vitest';
import type { NetworkStatus } from './network';
import {
	b64uDecode,
	contactAge,
	copySize,
	deriveNetworkState,
	fingerprintFromPublicKey,
	fingerprintsMatch,
	firstCopyNotice,
	formatCountdown,
	formatFingerprint,
	isCompleteFingerprint,
	joiningDeviceCount,
	looksLikeJoinLink,
	normalizeFingerprint,
	parseJoinLink,
	relativeTime,
	secondsUntil,
	transcriptNotice
} from './network-status';

function status(overrides: Partial<NetworkStatus> = {}): NetworkStatus {
	return {
		device: { device_id: 'self', fingerprint: '6668-7aad-f862-bd77-6c8f-c18b-8e9f-8e20' },
		enrolled: true,
		platform: 'linux',
		version: '1.0.0',
		instance_id: 'inst',
		network: {
			network_id: 'n1',
			name: 'Home',
			device_id: 'self',
			fingerprint: '6668-7aad-f862-bd77-6c8f-c18b-8e9f-8e20',
			founder_device_id: 'self',
			generation: 1,
			key_version: 1,
			bootstrap_state: 'ready',
			member_count: 1
		},
		members: [],
		pending_joins: [],
		sync: { records: {}, ops: 0, holes: [], peers: [], conflicts: 0 },
		recovery: { configured: false, created_at: null, key_version: null },
		hub: { linked: false },
		...overrides
	};
}

describe('deriveNetworkState', () => {
	it('is Offline when the status is stale or missing', () => {
		expect(deriveNetworkState(null)).toBe('offline');
		expect(deriveNetworkState(status(), true)).toBe('offline');
	});

	it('is Ready for an enrolled worker with no holes', () => {
		expect(deriveNetworkState(status())).toBe('ready');
	});

	it('is Syncing while bootstrapping or while streams have holes', () => {
		expect(deriveNetworkState(status({ network: { ...status().network!, bootstrap_state: 'syncing' } }))).toBe('syncing');
		expect(
			deriveNetworkState(
				status({ sync: { records: {}, ops: 1, holes: [{ origin_device_id: 'a', incarnation: 1, first_pending: 3, count: 2, watermark: 1 }], peers: [], conflicts: 0 } })
			)
		).toBe('syncing');
	});

	it('is Syncing during inbound apply or peer catch-up, and returns to Ready', () => {
		const sync = status().sync!;
		expect(deriveNetworkState(status({ sync: { ...sync, syncing: true } }))).toBe('syncing');
		const peer = { device_id: 'peer', base_url: null, endpoints: [], last_error: null, seconds_since_contact: 0, online: true, syncing: true };
		expect(deriveNetworkState(status({ sync: { ...sync, peers: [peer] } }))).toBe('syncing');
		expect(deriveNetworkState(status({ sync: { ...sync, peers: [{ ...peer, syncing: false }] } }))).toBe('ready');
	});

	it('is Needs local setup for a fresh worker', () => {
		expect(deriveNetworkState(status({ enrolled: false, network: undefined, enrollment: null }))).toBe('needs-setup');
	});

	it('is Joining while an invite is being collected or the account has yet to admit this worker', () => {
		const pending = { id: 'j', status: 'pending', endpoint_url: 'x', inviter_fingerprint: 'y', fingerprint: 'z', error: null };
		expect(deriveNetworkState(status({ enrolled: false, network: undefined, enrollment: pending }))).toBe('waiting');
		expect(deriveNetworkState(status({ enrolled: false, network: undefined, enrollment: { ...pending, status: 'syncing' } }))).toBe('waiting');
		expect(
			deriveNetworkState(status({ enrolled: false, network: undefined, enrollment: null, hub: { linked: true, registered: false, status: 'pending' } }))
		).toBe('waiting');
		const failed = { ...pending, status: 'failed', error: 'nope' };
		expect(deriveNetworkState(status({ enrolled: false, network: undefined, enrollment: failed }))).toBe('needs-setup');
	});

	it('is Joining while a worker switches to the account network that was founded elsewhere', () => {
		expect(deriveNetworkState(status({ hub: { linked: true, registered: false, status: 'conflict' } }))).toBe('waiting');
		expect(deriveNetworkState(status({ hub: { linked: true, registered: true, status: 'active' } }))).toBe('ready');
	});
});

describe('joiningDeviceCount', () => {
	it('adds joins and account-pending devices, but only when enrolled', () => {
		const join = { id: 'j', status: 'pending', device_id: 'd', name: 'n', platform: null, fingerprint: null, legacy_instance_id: null, created_at: null, expires_at: null };
		const device = { device_id: 'h', name: 'phone', platform: 'android', signing_public: 'AAAA' };
		expect(joiningDeviceCount(status({ pending_joins: [join], hub: { linked: true, pending_devices: [device, device] } }))).toBe(3);
		expect(joiningDeviceCount(status({ enrolled: false, pending_joins: [join] }))).toBe(0);
		expect(joiningDeviceCount(null)).toBe(0);
	});
});

describe('fingerprints', () => {
	it('formats the first 32 hex chars into 8 dashed groups', () => {
		expect(formatFingerprint('66687aadf862bd776c8fc18b8e9f8e20')).toBe('6668-7aad-f862-bd77-6c8f-c18b-8e9f-8e20');
		// A full 64-char digest is truncated to the first 128 bits.
		expect(formatFingerprint('66687aadf862bd776c8fc18b8e9f8e208a6b9b9b9b9b9b9b9b9b9b9b9b9b9b9b')).toBe('6668-7aad-f862-bd77-6c8f-c18b-8e9f-8e20');
		// Already-dashed / upper-case input normalises.
		expect(formatFingerprint('6668-7AAD-F862-BD77-6C8F-C18B-8E9F-8E20')).toBe('6668-7aad-f862-bd77-6c8f-c18b-8e9f-8e20');
	});

	it('normalises and compares regardless of dashes, spaces and case', () => {
		expect(normalizeFingerprint(' 6668-7AAD f862 ')).toBe('66687aadf862');
		expect(fingerprintsMatch('6668-7aad-f862-bd77-6c8f-c18b-8e9f-8e20', '66687AADF862BD776C8FC18B8E9F8E20')).toBe(true);
		expect(fingerprintsMatch('6668-7aad-f862-bd77-6c8f-c18b-8e9f-8e20', '6668-7aad')).toBe(false);
		expect(fingerprintsMatch('', '')).toBe(false);
		expect(isCompleteFingerprint('6668-7aad-f862-bd77-6c8f-c18b-8e9f-8e20')).toBe(true);
		expect(isCompleteFingerprint('6668-7aad')).toBe(false);
	});

	it('decodes unpadded base64url and rejects the standard alphabet', () => {
		// 32 zero bytes → 43 'A's in base64url.
		expect(Array.from(b64uDecode('A'.repeat(43)))).toEqual(new Array(32).fill(0));
		expect(Array.from(b64uDecode('_-8'))).toEqual([0xff, 0xef]);
		expect(() => b64uDecode('AA==')).toThrow();
		expect(() => b64uDecode('A+B')).toThrow();
	});

	it('computes the fingerprint of a base64url public key (known vector: SHA-256 of 32 zero bytes)', async () => {
		// sha256(0x00 * 32) = 66687aadf862bd776c8fc18b8e9f8e20 08971485 6e6f...
		expect(await fingerprintFromPublicKey('A'.repeat(43))).toBe('6668-7aad-f862-bd77-6c8f-c18b-8e9f-8e20');
	});

	it('fails loudly without WebCrypto', async () => {
		await expect(fingerprintFromPublicKey('A'.repeat(43), null)).rejects.toThrow(/WebCrypto/);
	});
});

describe('join links', () => {
	it('recognises and parses the QR payload', () => {
		const link = 'shuffle://network/join?endpoint=https%3A%2F%2F10.0.0.5%3A8000&code=ABCD-EFGH-JKLM&fp=6668-7aad-f862-bd77-6c8f-c18b-8e9f-8e20';
		expect(looksLikeJoinLink(link)).toBe(true);
		expect(looksLikeJoinLink('ABCD-EFGH-JKLM')).toBe(false);
		expect(parseJoinLink(link)).toEqual({
			endpoint: 'https://10.0.0.5:8000',
			code: 'ABCD-EFGH-JKLM',
			fingerprint: '6668-7aad-f862-bd77-6c8f-c18b-8e9f-8e20'
		});
		expect(parseJoinLink('ABCD-EFGH-JKLM')).toBeNull();
	});
});

describe('time helpers', () => {
	const now = Date.parse('2026-09-08T12:00:00Z');

	it('counts down to an expiry and formats m:ss', () => {
		expect(secondsUntil('2026-09-08T12:02:05Z', now)).toBe(125);
		expect(secondsUntil('2026-09-08T11:00:00Z', now)).toBe(0);
		expect(secondsUntil(null, now)).toBe(0);
		expect(formatCountdown(125)).toBe('2:05');
		expect(formatCountdown(-3)).toBe('0:00');
	});

	it('renders relative times and contact ages', () => {
		expect(relativeTime(null, now)).toBe('never');
		expect(relativeTime('2026-09-08T11:59:50Z', now)).toBe('just now');
		expect(relativeTime('2026-09-08T11:30:00Z', now)).toBe('30m ago');
		expect(relativeTime('2026-09-08T09:00:00Z', now)).toBe('3h ago');
		expect(relativeTime('2026-09-05T12:00:00Z', now)).toBe('3d ago');
		expect(contactAge(null)).toBe('no contact yet');
		expect(contactAge(2)).toBe('just now');
		expect(contactAge(42)).toBe('42s ago');
		expect(contactAge(600)).toBe('10m ago');
		expect(contactAge(7200)).toBe('2h ago');
	});
});

describe('firstCopyNotice', () => {
	const syncing = (hub: Partial<NonNullable<NetworkStatus['hub']>> = {}) =>
		status({ network: { ...status().network!, bootstrap_state: 'syncing' }, hub: { linked: true, ...hub } });
	const copy = (fields: object) => ({
		peer: 'peer-a', peer_name: 'Unraid', phase: 'downloading' as const, pages_done: 0, pages_total: 0,
		bytes_done: 0, bytes_total: 0, error: null, ...fields
	});

	it('says nothing once the worker is ready or outside a network', () => {
		expect(firstCopyNotice(status())).toBeNull();
		expect(firstCopyNotice(status({ enrolled: false, network: undefined }))).toBeNull();
		expect(firstCopyNotice(null)).toBeNull();
	});

	it('counts pages and size while downloading', () => {
		const notice = firstCopyNotice(syncing({ first_copy: copy({
			pages_done: 12458, pages_total: 22362, bytes_done: 4.7 * 1024 ** 3, bytes_total: 8.5 * 1024 ** 3
		}) }));
		expect(notice?.message).toBe('Copying your data from Unraid: 12,458 of 22,362 pages (4.7 of 8.5 GB).');
		expect(notice?.fraction).toBeCloseTo(12458 / 22362);
		expect(notice?.severity).toBe('info');
	});

	it('names the applying and failed phases, and the wait before a copy starts', () => {
		expect(firstCopyNotice(syncing({ first_copy: copy({ phase: 'applying' }) }))?.message)
			.toBe('Saving the data copied from Unraid. Sessions and workers appear when this finishes.');
		const failed = firstCopyNotice(syncing({ first_copy: copy({ phase: 'failed', error: 'Peer transfer interrupted.' }) }));
		expect(failed?.message).toBe('Copying from Unraid stopped: Peer transfer interrupted. It will retry on its own.');
		expect(failed?.severity).toBe('warning');
		expect(firstCopyNotice(syncing({ first_copy: copy({ phase: 'failed', error: 'Timed out', pages_done: 1200 }) }))?.message)
			.toBe('Copying from Unraid stopped: Timed out. It will retry on its own and keep the 1,200 pages already copied.');
		expect(firstCopyNotice(syncing({ first_copy: copy({ phase: 'preparing', peer_name: null }) }))?.message)
			.toBe('Waiting for another worker to prepare a copy of your data.');
		expect(firstCopyNotice(syncing({ last_error: 'Waiting for another worker to come online to copy your data' }))?.message)
			.toBe('Waiting for another worker to come online to copy your data');
	});

	it('sizes small copies in MB', () => {
		expect(copySize(1.5 * 1024 ** 2, 20 * 1024 ** 2)).toBe('1.5 of 20.0 MB');
	});
});

describe('transcriptNotice', () => {
	const ready = (transcripts: object | null) =>
		status({ hub: { linked: true, transcripts: transcripts as never } });
	const copy = (fields: object = {}) => ({
		phase: 'transcripts', peer: 'peer-a', peer_name: 'Unraid', sessions_done: 312, sessions_total: 1152,
		events_copied: 40000, error: null, ...fields
	});

	it('counts sessions while transcripts download on a ready worker', () => {
		const notice = transcriptNotice(ready(copy()));
		expect(notice?.title).toBe('Downloading transcripts');
		expect(notice?.message).toBe(
			'Downloading transcripts from Unraid: 312 of 1,152 sessions. Older chats fill in as they arrive; ' +
				'a chat you open is fetched first.'
		);
		expect(notice?.fraction).toBeCloseTo(312 / 1152);
		expect(notice?.severity).toBe('info');
	});

	it('says why it paused, and nothing when there is nothing left or the first copy still runs', () => {
		const paused = transcriptNotice(ready(copy({ error: 'HTTP 503.' })));
		expect(paused?.message).toBe(
			'Downloading transcripts from Unraid paused at 312 of 1,152 sessions: HTTP 503. It will retry on its own.'
		);
		expect(paused?.severity).toBe('warning');
		expect(transcriptNotice(ready(null))).toBeNull();
		expect(transcriptNotice(status({ network: { ...status().network!, bootstrap_state: 'syncing' },
			hub: { linked: true, transcripts: copy() as never } }))).toBeNull();
	});

	it('shows how far saving the first copy got', () => {
		const notice = firstCopyNotice(status({ network: { ...status().network!, bootstrap_state: 'syncing' },
			hub: { linked: true, first_copy: { peer: 'peer-a', peer_name: 'Unraid', phase: 'applying', pages_done: 60,
				pages_total: 60, bytes_done: 0, bytes_total: 0, apply_done: 15, apply_total: 60, error: null } } }));
		expect(notice?.fraction).toBeCloseTo(0.25);
	});
});
