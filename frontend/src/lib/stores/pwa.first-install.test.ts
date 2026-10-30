/**
 * #908: sw.js claims the page and posts SW_UPDATED on every activation,
 * the first install of a fresh visit included. Half a second into a visit
 * the page is inside the start-up grace, so treating that as an update
 * reloaded every fresh secure-origin visit once. Only a different served
 * build is an update. Its own file: `updateReady` is module state that
 * pwa.sw-update.test.ts leaves set.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

vi.mock('$lib/services/desktop', () => ({ isDesktop: false }));
vi.mock('$lib/api/runtime', () => ({ isBundledMobile: () => false }));
const kit = vi.hoisted(() => ({ newBuild: false, check: vi.fn() }));
vi.mock('$app/state', () => ({ updated: { current: false, check: kit.check } }));

import { pwa } from './pwa.svelte';
import { installAppUpdateWatch } from '$lib/services/app-update-watch';
import { notifications, notify } from '$lib/stores/notifications';

const reload = vi.fn();
const swListeners = new Map<string, Array<(event?: unknown) => void>>();
let teardown: () => void = () => {};
const banners = () => notifications.current.live.filter((n) => n.category === 'app-update');

/** What sw.js does on activation: claim (controllerchange), then post SW_UPDATED. */
async function claimAndPost() {
	for (const listener of swListeners.get('controllerchange') ?? []) listener();
	for (const listener of swListeners.get('message') ?? []) listener({ data: { type: 'SW_UPDATED' } });
	// Let the version check answer (fake timers cover Date only).
	await new Promise((done) => setTimeout(done, 0));
}

beforeEach(async () => {
	vi.useFakeTimers({ toFake: ['Date'] });
	vi.setSystemTime(1_000_000);
	Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
	vi.stubGlobal('location', { ...window.location, reload });
	reload.mockClear();
	kit.check.mockReset();
	kit.check.mockImplementation(async () => kit.newBuild);
	sessionStorage.clear();
	for (const n of notifications.current.live) notify.dismissLive(n.id);
	Object.defineProperty(navigator, 'serviceWorker', {
		configurable: true,
		value: {
			register: vi.fn(async () => ({ update: async () => {} })),
			addEventListener: (type: string, listener: (event?: unknown) => void) => {
				swListeners.set(type, [...(swListeners.get(type) ?? []), listener]);
			},
			removeEventListener() {},
			controller: null
		}
	});
	// A fresh visit: the watcher, then the registration; the claim lands 0.5 s in.
	swListeners.clear();
	teardown = installAppUpdateWatch();
	await pwa.registerServiceWorker();
	vi.setSystemTime(Date.now() + 500);
});

afterEach(() => {
	teardown();
	teardown = () => {};
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

it('the first install on the same build neither reloads, shows a banner nor flags an update', async () => {
	kit.newBuild = false;
	await claimAndPost();
	expect(kit.check).toHaveBeenCalled();
	expect(reload).not.toHaveBeenCalled();
	expect(banners()).toHaveLength(0);
	expect(pwa.current.updateReady).toBe(false);
});

it('a first install that brings a different build still reloads the fresh page', async () => {
	kit.newBuild = true;
	await claimAndPost();
	expect(reload).toHaveBeenCalledTimes(1);
	expect(pwa.current.updateReady).toBe(true);
});
