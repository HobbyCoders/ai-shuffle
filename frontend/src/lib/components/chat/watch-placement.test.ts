/** Where watches run (#565): tiers, automatic choice, move targets, the preference line. */
import { describe, it, expect } from 'vitest';
import type { Install } from '$lib/api/installs';
import {
	automaticWorker,
	moveOptions,
	placementOf,
	preferenceSummary,
	tierLabel,
	transferTarget,
	workerChoices,
	workerTier
} from './watch-placement';

function install(id: string, name: string, over: Partial<Install> = {}): Install {
	return { id, device_id: id, legacy_instance_id: `inst-${name}`, device_name: name, base_url: null, platform: 'linux', version: null, capabilities: {}, last_seen: null, online: true, ...over };
}

const desktop = install('dev_desk', 'Desk', { platform: 'windows', is_self: true });
const server = install('dev_srv', 'Box');
const always = install('dev_mac', 'Mini', { platform: 'darwin', capabilities: { always_on: true } });
const off = install('dev_off', 'Gamer', { platform: 'windows', online: false });

describe('workerTier', () => {
	it('mirrors placement: marked always on, then a Linux server, else may sleep', () => {
		expect(workerTier(always)).toBe('always_on');
		expect(workerTier(server)).toBe('server');
		expect(workerTier(install('x', 'LinuxDesk', { capabilities: { desktop: true } }))).toBe('may_sleep');
		expect(workerTier(desktop)).toBe('may_sleep');
		expect(tierLabel('always_on')).toBe('Always on');
	});
});

describe('automaticWorker', () => {
	it('prefers an online always-on worker, then a server, never one that may sleep', () => {
		expect(automaticWorker(workerChoices([desktop, server, always]))?.name).toBe('Mini');
		expect(automaticWorker(workerChoices([desktop, server]))?.name).toBe('Box');
		expect(automaticWorker(workerChoices([desktop, { ...server, online: false }]))).toBeNull();
	});
});

describe('workerChoices', () => {
	it('lists this worker first, then by name, and leaves out revoked ones', () => {
		const names = workerChoices([server, off, desktop, install('r', 'Gone', { revoked_at: '2026-01-01' })]).map((w) => w.name);
		expect(names).toEqual(['Desk', 'Box', 'Gamer']);
	});
});

describe('placementOf', () => {
	it('finds the executing worker by device id, else by install id', () => {
		expect(placementOf({ placementDeviceId: 'dev_srv', workerId: 'inst-Box' }, [server]).worker?.name).toBe('Box');
		expect(placementOf({ placementDeviceId: null, workerId: 'inst-Box' }, [server]).worker?.name).toBe('Box');
		expect(placementOf({ placementDeviceId: 'dev_gone', workerId: 'inst-gone' }, [server])).toEqual({ worker: null, reportedId: 'dev_gone' });
		expect(placementOf({ placementDeviceId: 'dev_srv' }, null).worker).toBeNull();
	});
});

describe('transferTarget', () => {
	it('reads the device a fenced row moves to', () => {
		expect(transferTarget('transfer:dev_srv')).toBe('dev_srv');
		expect(transferTarget('transfer:')).toBe('');
		expect(transferTarget('inst-Box')).toBeNull();
		expect(transferTarget(null)).toBeNull();
	});
});

describe('moveOptions', () => {
	it('offers every other worker and says why an offline one cannot take it', () => {
		const options = moveOptions(workerChoices([desktop, server, off]), 'dev_desk');
		expect(options.map((o) => [o.name, o.unavailable])).toEqual([['Box', null], ['Gamer', 'Offline']]);
	});
});

describe('preferenceSummary', () => {
	const all = [desktop, server, off];
	const choices = workerChoices(all);

	it('automatic names the worker it picks now, or says none is online', () => {
		expect(preferenceSummary(null, choices, all)).toMatchObject({ label: 'Automatic · Box', problem: null });
		const none = preferenceSummary(null, workerChoices([desktop]), [desktop]);
		expect(none.effective).toBeNull();
		expect(none.problem).toMatch(/ask you to choose a worker/);
	});

	it('a chosen worker that is offline or gone is refused, never replaced', () => {
		expect(preferenceSummary('inst-Box', choices, all)).toMatchObject({ label: 'Box', problem: null });
		const offline = preferenceSummary('dev_off', choices, all);
		expect(offline).toMatchObject({ label: 'Gamer (offline)', effective: null });
		expect(offline.problem).toMatch(/never fall back silently/);
		expect(preferenceSummary('dev_gone', choices, all)).toMatchObject({ label: 'A removed worker', effective: null });
	});
});
