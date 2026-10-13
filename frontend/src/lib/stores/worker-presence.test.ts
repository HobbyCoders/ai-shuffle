import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

const { listInstallsMock } = vi.hoisted(() => ({ listInstallsMock: vi.fn() }));
vi.mock('$lib/api/installs', () => ({ listInstalls: listInstallsMock }));

import {
	PRESENCE_REFRESH_MS,
	presenceOf,
	refreshWorkerPresence,
	resetWorkerPresence,
	watchWorkerPresence,
	workerInstalls
} from './worker-presence';
import type { Install } from '$lib/api/installs';

const install = (over: Partial<Install>): Install => ({
	id: 'id', device_name: 'Worker', base_url: null, platform: 'linux', version: null,
	capabilities: {}, last_seen: null, online: true, ...over
});

beforeEach(() => {
	vi.useFakeTimers();
	listInstallsMock.mockReset();
	listInstallsMock.mockResolvedValue({ installs: [install({ id: 'unraid', online: true })] });
});

afterEach(() => {
	resetWorkerPresence();
	vi.useRealTimers();
});

describe('worker presence store', () => {
	it('loads at once when watched, refreshes on an interval, and stops when released', async () => {
		const release = watchWorkerPresence();
		await vi.advanceTimersByTimeAsync(0);
		expect(get(workerInstalls)?.[0].online).toBe(true);
		expect(listInstallsMock).toHaveBeenCalledTimes(1);

		listInstallsMock.mockResolvedValue({ installs: [install({ id: 'unraid', online: false })] });
		await vi.advanceTimersByTimeAsync(PRESENCE_REFRESH_MS);
		expect(get(workerInstalls)?.[0].online).toBe(false);
		expect(listInstallsMock).toHaveBeenCalledTimes(2);

		release();
		await vi.advanceTimersByTimeAsync(PRESENCE_REFRESH_MS * 3);
		expect(listInstallsMock).toHaveBeenCalledTimes(2);
	});

	it('keeps polling until the last watcher releases', async () => {
		const a = watchWorkerPresence();
		const b = watchWorkerPresence();
		await vi.advanceTimersByTimeAsync(0);
		a();
		a(); // a second release of the same watcher is ignored
		await vi.advanceTimersByTimeAsync(PRESENCE_REFRESH_MS);
		expect(listInstallsMock).toHaveBeenCalledTimes(2);
		b();
		await vi.advanceTimersByTimeAsync(PRESENCE_REFRESH_MS);
		expect(listInstallsMock).toHaveBeenCalledTimes(2);
	});

	it('shares one request between concurrent refreshes and keeps the last list on failure', async () => {
		await Promise.all([refreshWorkerPresence(), refreshWorkerPresence()]);
		expect(listInstallsMock).toHaveBeenCalledTimes(1);
		listInstallsMock.mockRejectedValueOnce(new Error('down'));
		await expect(refreshWorkerPresence()).rejects.toThrow('down');
		expect(get(workerInstalls)?.[0].id).toBe('unraid');
	});
});

describe('presenceOf', () => {
	const installs = [
		install({ id: 'dev_unraid', device_id: 'dev_unraid', legacy_instance_id: 'ai-shuffle-unraid', device_name: 'ServerWorker', online: false }),
		install({ id: 'pop', device_name: 'linux-worker', online: true })
	];

	it('matches an owner by install, device or legacy instance id', () => {
		expect(presenceOf(installs, ['dev_unraid'])).toBe(false);
		expect(presenceOf(installs, [null, 'ai-shuffle-unraid'])).toBe(false);
		expect(presenceOf(installs, ['pop'])).toBe(true);
	});

	it('falls back to a unique display name, else reports unknown', () => {
		expect(presenceOf(installs, [], 'ServerWorker')).toBe(false);
		expect(presenceOf(installs, ['missing'], 'Elsewhere')).toBeNull();
		expect(presenceOf([...installs, install({ id: 'x', device_name: 'ServerWorker' })], [], 'ServerWorker')).toBeNull();
		expect(presenceOf(null, ['pop'])).toBeNull();
	});
});
