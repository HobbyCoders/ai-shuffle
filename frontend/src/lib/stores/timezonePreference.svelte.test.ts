/**
 * Timezone preference — local cache, server sync binding (with IANA
 * validation of the synced value) and resolution of "local".
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

type Binding = { read: () => unknown; apply: (value: unknown) => void };

const sync = vi.hoisted(() => ({
	bindings: new Map<string, Binding>(),
	save: vi.fn(),
	refresh: vi.fn()
}));

vi.mock('$lib/services/preferences', () => ({
	preferenceSync: {
		register: (key: string, binding: Binding) => sync.bindings.set(key, binding),
		save: sync.save,
		refresh: sync.refresh
	}
}));

const STORAGE_KEY = 'ai-shuffle-timezone';

async function load() {
	vi.resetModules();
	sync.bindings.clear();
	return (await import('./timezonePreference.svelte')).timezonePreference;
}

beforeEach(() => {
	localStorage.clear();
	sync.save.mockReset();
	sync.refresh.mockReset();
});

describe('timezonePreference', () => {
	it('defaults to "local" and resolves it to the browser zone', async () => {
		const tz = await load();
		expect(tz.current).toBe('local');
		expect(tz.resolveTimezone()).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone);
	});

	it('starts from the cached value', async () => {
		localStorage.setItem(STORAGE_KEY, 'Asia/Tokyo');
		const tz = await load();
		expect(tz.current).toBe('Asia/Tokyo');
		expect(tz.resolveTimezone()).toBe('Asia/Tokyo');
	});

	it('setTimezone notifies, caches and saves to the server', async () => {
		const tz = await load();
		const seen: string[] = [];
		tz.subscribe((v) => seen.push(v));
		tz.setTimezone('Europe/Berlin');
		expect(seen).toEqual(['local', 'Europe/Berlin']);
		expect(localStorage.getItem(STORAGE_KEY)).toBe('Europe/Berlin');
		expect(sync.save).toHaveBeenCalledWith('timezone', 'Europe/Berlin');
	});

	it('load and syncFromServer refresh the server key', async () => {
		const tz = await load();
		await tz.load();
		await tz.syncFromServer();
		expect(sync.refresh).toHaveBeenCalledTimes(2);
		expect(sync.refresh).toHaveBeenCalledWith('timezone');
	});

	it('applies a valid synced zone and caches it', async () => {
		const tz = await load();
		const binding = sync.bindings.get('timezone')!;
		binding.apply('America/New_York');
		expect(tz.current).toBe('America/New_York');
		expect(binding.read()).toBe('America/New_York');
		expect(localStorage.getItem(STORAGE_KEY)).toBe('America/New_York');
		expect(sync.save).not.toHaveBeenCalled();
	});

	it('falls back to "local" for an unknown zone or a non-string value', async () => {
		const tz = await load();
		const binding = sync.bindings.get('timezone')!;
		binding.apply('Mars/Olympus_Mons');
		expect(tz.current).toBe('local');
		tz.setTimezone('Asia/Tokyo');
		binding.apply(42);
		expect(tz.current).toBe('local');
		expect(() => tz.resolveTimezone()).not.toThrow();
	});
});
