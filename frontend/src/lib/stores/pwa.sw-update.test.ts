/**
 * #711: a service-worker activation is news for the app-update monitor, not
 * a reload of its own, so it obeys the same cap and loop guard as every
 * other update path.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

vi.mock('$lib/services/desktop', () => ({ isDesktop: false }));
vi.mock('$lib/api/runtime', () => ({ isBundledMobile: () => false }));
const kit = vi.hoisted(() => ({ check: vi.fn(async () => false) }));
vi.mock('$app/state', () => ({ updated: { current: false, check: kit.check } }));

import { pwa } from './pwa.svelte';
import { installAppUpdateWatch } from '$lib/services/app-update-watch';
import { AUTO_RELOAD_COOLDOWN_MS, RESUME_GRACE_MS, registerReloadBlocker } from '$lib/services/app-update';
import { notifications, notify } from '$lib/stores/notifications';

let visibility: DocumentVisibilityState = 'visible';
const reload = vi.fn();
const swListeners = new Map<string, Array<(event?: unknown) => void>>();
let teardown: () => void = () => {};
const banners = () => notifications.current.live.filter((n) => n.category === 'app-update');

function activate() {
	for (const listener of swListeners.get('controllerchange') ?? []) listener();
}

/** A fresh page: the watcher and the service worker registration it makes. */
async function loadPage() {
	teardown();
	swListeners.clear();
	teardown = installAppUpdateWatch();
	await pwa.registerServiceWorker();
	// The page has been open for a while.
	vi.setSystemTime(Date.now() + RESUME_GRACE_MS + 1);
}

beforeEach(async () => {
	vi.useFakeTimers({ toFake: ['Date'] });
	vi.setSystemTime(1_000_000);
	visibility = 'visible';
	Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility });
	vi.stubGlobal('location', { ...window.location, reload });
	reload.mockClear();
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
	await loadPage();
});

afterEach(() => {
	teardown();
	teardown = () => {};
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

it('a hidden tab reloads once; a second activation inside the cooldown does not reload again', async () => {
	visibility = 'hidden';
	activate();
	expect(reload).toHaveBeenCalledTimes(1);

	// The reloaded page meets another activation (replicas on different
	// builds) a minute later, still hidden.
	vi.setSystemTime(Date.now() + 60_000);
	await loadPage();
	activate();
	expect(reload).toHaveBeenCalledTimes(1);
	expect(banners()).toHaveLength(1);
});

it('the cooldown is per build and time: another build after it may reload again', async () => {
	sessionStorage.setItem('ai-shuffle-update-auto-reload', JSON.stringify({ from: 'older-build', at: Date.now() - AUTO_RELOAD_COOLDOWN_MS }));
	visibility = 'hidden';
	activate();
	expect(reload).toHaveBeenCalledTimes(1);
});

it('a visible tab gets the banner and reloads on the next hide', () => {
	activate();
	expect(reload).not.toHaveBeenCalled();
	expect(banners()).toHaveLength(1);
	expect(pwa.current.updateReady).toBe(true);
	visibility = 'hidden';
	document.dispatchEvent(new Event('visibilitychange'));
	expect(reload).toHaveBeenCalledTimes(1);
});

it('never reloads a hidden tab over something a reload would lose', () => {
	const undo = registerReloadBlocker(() => 'voice-call');
	try {
		visibility = 'hidden';
		activate();
		for (const listener of swListeners.get('message') ?? []) listener({ data: { type: 'SW_UPDATED' } });
		expect(reload).not.toHaveBeenCalled();
		expect(banners()).toHaveLength(1);
	} finally {
		undo();
	}
});
