import { describe, expect, it } from 'vitest';
import type { Install } from '$lib/api/installs';
import type { Session } from '$lib/api/sessions';
import { connectionWorkers, switchRoute, type ConnectionWorker } from './worker-connection';

const install = (id: string, extra: Partial<Install> = {}): Install => ({
	id, device_name: id === 'pc' ? 'DesktopWorker' : 'ServerWorker', base_url: `https://${id}.test`, platform: null,
	version: null, capabilities: {}, last_seen: null, online: true, ...extra
});
const session = (id: string, extra: Partial<Session>): Session => ({ id, parent_session_id: null, ...extra } as Session);

describe('"Connected to" workers', () => {
	it('lists the current worker first with running-chat counts', () => {
		const rows = connectionWorkers(
			[install('unraid'), install('pc', { is_self: true }), install('old', { online: false, device_name: 'Laptop' })],
			[
				session('a', { is_remote: false, work_started_at: '2026-10-04T10:00:00Z' }),
				session('b', { is_remote: false }),
				session('c', { is_remote: true, owner_device_id: 'unraid', remote_runtime_state: 'working' }),
				session('d', { is_remote: false, parent_session_id: 'a', work_started_at: 'x' })
			],
			new Set(['b'])
		);
		expect(rows.map((r) => [r.name, r.current, r.running, r.online])).toEqual([
			['DesktopWorker', true, 2, true],
			['ServerWorker', false, 1, true],
			['Laptop', false, 0, false]
		]);
	});

	it('leaves out revoked and unapproved workers', () => {
		const rows = connectionWorkers([install('pc', { is_self: true }), install('x', { approved: false }), install('y', { revoked_at: 'now' })], [], new Set());
		expect(rows).toHaveLength(1);
	});
});

describe('switching the app\'s connection', () => {
	const unraid: ConnectionWorker = { id: 'unraid', name: 'ServerWorker', online: true, current: false, running: 1, baseUrl: 'https://unraid.test/' };
	const page = 'https://pc.test/mobile/chat?x=1';

	it('asks the app to reconnect when it offers the switch', () => {
		expect(switchRoute(unraid, { inApp: true, canSwitch: true, pageUrl: page }))
			.toEqual({ kind: 'app', url: 'https://pc.test/mobile/?connect=https%3A%2F%2Funraid.test' });
	});

	it('opens the worker\'s own phone UI in a plain browser', () => {
		expect(switchRoute(unraid, { inApp: false, canSwitch: false, pageUrl: page }))
			.toEqual({ kind: 'browser', url: 'https://unraid.test/mobile/' });
	});

	it('points to the app menu when the app cannot switch from the page', () => {
		expect(switchRoute(unraid, { inApp: true, canSwitch: false, pageUrl: page }).kind).toBe('unavailable');
	});

	it('does nothing for the current worker and refuses an offline one', () => {
		expect(switchRoute({ ...unraid, current: true }, { inApp: false, canSwitch: false, pageUrl: page })).toEqual({ kind: 'current' });
		expect(switchRoute({ ...unraid, online: false }, { inApp: false, canSwitch: false, pageUrl: page }))
			.toEqual({ kind: 'unavailable', reason: 'ServerWorker is offline' });
	});
});
