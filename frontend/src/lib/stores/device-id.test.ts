import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

/**
 * Phase 3.3 — per-tab session scoping.
 *
 * The per-tab active-session slot lives CLIENT-side in sessionStorage (which is
 * already per-tab), so it needs no id in its key and creates zero server rows.
 * device-id.ts only owns the key constant here; the read/write logic lives in
 * session-switcher (see session-switcher-persist.test.ts). These tests pin the
 * key string and confirm the device id is untouched by the redesign.
 *
 * device-id.ts caches the device id at module scope, so tests that assert on it
 * re-import fresh (vi.resetModules) after seeding storage.
 */

async function freshModule() {
	vi.resetModules();
	return import('./device-id');
}

beforeEach(() => {
	localStorage.clear();
	sessionStorage.clear();
});

afterEach(() => {
	vi.restoreAllMocks();
});

describe('device-id · TAB_ACTIVE_SESSION_KEY', () => {
	it('exposes a stable, tab-id-free sessionStorage key', async () => {
		const { TAB_ACTIVE_SESSION_KEY } = await freshModule();
		expect(TAB_ACTIVE_SESSION_KEY).toBe('ai-shuffle-active-session');
		// No per-tab id is baked into the key — sessionStorage is already per-tab.
		expect(TAB_ACTIVE_SESSION_KEY).not.toMatch(/tab-/);
	});
});

describe('device-id · getOrCreateDeviceId (untouched by 3.3)', () => {
	it('creates and persists a device id in localStorage', async () => {
		const { getOrCreateDeviceId } = await freshModule();
		const id = getOrCreateDeviceId();
		expect(id).toMatch(/^device-/);
		expect(localStorage.getItem('ai-shuffle-device-id')).toBe(id);
	});

	it('reuses the existing localStorage device id', async () => {
		localStorage.setItem('ai-shuffle-device-id', 'device-fixed');
		const { getOrCreateDeviceId } = await freshModule();
		expect(getOrCreateDeviceId()).toBe('device-fixed');
	});

	it('does not touch sessionStorage', async () => {
		const { getOrCreateDeviceId } = await freshModule();
		getOrCreateDeviceId();
		expect(sessionStorage.getItem('ai-shuffle-active-session')).toBeNull();
	});
});
