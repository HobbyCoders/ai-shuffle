/**
 * The browser wiring of #707: the events that trigger a version check, the
 * one "App updated" banner and the reload.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const kit = vi.hoisted(() => ({ check: vi.fn(async () => true) }));
vi.mock('$app/state', () => ({ updated: { current: false, check: kit.check } }));
vi.mock('$lib/api/client', () => ({ api: { get: vi.fn(), post: vi.fn(() => Promise.resolve({})), delete: vi.fn() } }));
vi.mock('$lib/stores/auth.svelte', () => ({ auth: { current: { status: { authenticated: true } } } }));

import { installAppUpdateWatch } from './app-update-watch';
import { APP_UPDATED_MESSAGE, APP_UPDATE_READY_MESSAGE, RESUME_GRACE_MS, registerReloadBlocker, reportServerReconnected } from './app-update';
import { notifications, notify } from '$lib/stores/notifications';

let visibility: DocumentVisibilityState = 'visible';
const reload = vi.fn();
let teardown: () => void = () => {};
const banners = () => notifications.current.live.filter((n) => n.category === 'app-update');
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function setVisibility(next: DocumentVisibilityState) {
	visibility = next;
	document.dispatchEvent(new Event('visibilitychange'));
}

beforeEach(() => {
	vi.useFakeTimers({ toFake: ['Date'] });
	vi.setSystemTime(1_000_000);
	visibility = 'visible';
	Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility });
	vi.stubGlobal('location', { ...window.location, reload });
	reload.mockClear();
	kit.check.mockClear().mockResolvedValue(true);
	for (const n of notifications.current.live) notify.dismissLive(n.id);
	teardown = installAppUpdateWatch();
	// The page has been in use for a while.
	vi.setSystemTime(1_000_000 + RESUME_GRACE_MS + 1);
});

afterEach(() => {
	teardown();
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

it('the phone app coming back to the foreground on an old build reloads', async () => {
	setVisibility('hidden');
	setVisibility('visible');
	await flush();
	expect(kit.check).toHaveBeenCalledTimes(1);
	expect(reload).toHaveBeenCalledTimes(1);
	expect(banners()).toEqual([]);
});

it('a reconnect while the user is on the page offers one neutral banner, then reloads on hide', async () => {
	reportServerReconnected();
	await flush();
	vi.setSystemTime(Date.now() + 60_000);
	reportServerReconnected();
	await flush();
	expect(reload).not.toHaveBeenCalled();
	expect(banners()).toEqual([expect.objectContaining({ severity: 'info', message: APP_UPDATE_READY_MESSAGE, action: expect.objectContaining({ label: 'Reload' }) })]);

	setVisibility('hidden');
	expect(reload).toHaveBeenCalledTimes(1);
});

it("the banner's Reload reloads even over a draft", async () => {
	const undo = registerReloadBlocker(() => 'composer-draft');
	try {
		setVisibility('hidden');
		setVisibility('visible');
		await flush();
		expect(reload).not.toHaveBeenCalled();
		banners()[0].action!.onClick();
		expect(reload).toHaveBeenCalledTimes(1);
	} finally {
		undo();
	}
});

it('a lazy chunk that would not load upgrades the banner to "a feature broke"', async () => {
	reportServerReconnected();
	await flush();
	window.dispatchEvent(new Event('vite:preloadError'));
	await flush();
	expect(banners()).toEqual([expect.objectContaining({ severity: 'warning', message: APP_UPDATED_MESSAGE })]);
});

it('does not repeat what the voice call outcome banner already says', async () => {
	notify.banner(APP_UPDATED_MESSAGE, { category: 'app-update', action: { label: 'Reload', onClick: () => {} } });
	window.dispatchEvent(new Event('vite:preloadError'));
	await flush();
	expect(notifications.current.live.filter((n) => n.message === APP_UPDATED_MESSAGE)).toHaveLength(1);
});

it('stays quiet while the server runs this build', async () => {
	kit.check.mockResolvedValue(false);
	setVisibility('hidden');
	setVisibility('visible');
	window.dispatchEvent(new Event('online'));
	await flush();
	expect(reload).not.toHaveBeenCalled();
	expect(banners()).toEqual([]);
});
