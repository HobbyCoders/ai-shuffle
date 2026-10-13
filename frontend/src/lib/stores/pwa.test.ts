import { afterEach, beforeEach, expect, it, vi } from 'vitest';

vi.mock('$lib/services/desktop', () => ({ isDesktop: false }));
const app = vi.hoisted(() => ({ bundled: false }));
vi.mock('$lib/api/runtime', () => ({ isBundledMobile: () => app.bundled }));

const unregister = vi.fn(async () => true);
const getRegistrations = vi.fn(async () => [{ unregister }, { unregister }]);
const register = vi.fn(async () => ({ addEventListener() {}, installing: null, waiting: null, active: null, update: async () => {} }));
const baseAgent = navigator.userAgent;

function setUserAgent(value: string) {
	Object.defineProperty(navigator, 'userAgent', { configurable: true, get: () => value });
}

beforeEach(() => {
	register.mockClear();
	unregister.mockClear();
	app.bundled = false;
	Object.defineProperty(navigator, 'serviceWorker', {
		configurable: true,
		value: { register, getRegistrations, addEventListener() {}, removeEventListener() {}, controller: null }
	});
});

afterEach(() => {
	setUserAgent(baseAgent);
	vi.restoreAllMocks();
});

it('R10-10: a desktop pop-up (no preload bridge, desktop user agent) does not register the service worker', async () => {
	setUserAgent(`${baseAgent} AIShuffleDesktop/1.2.3`);
	const { pwa } = await import('./pwa.svelte');
	expect(await pwa.registerServiceWorker()).toBeNull();
	expect(register).not.toHaveBeenCalled();
});

it('an ordinary browser still registers the service worker', async () => {
	setUserAgent(baseAgent);
	vi.spyOn(console, 'log').mockImplementation(() => {});
	const { pwa } = await import('./pwa.svelte');
	await pwa.registerServiceWorker();
	expect(register).toHaveBeenCalledWith('/sw.js', { scope: '/', updateViaCache: 'none' });
});

it('the phone apps get no service worker and drop one an older app left on the worker origin', async () => {
	app.bundled = true;
	const { pwa } = await import('./pwa.svelte');
	expect(await pwa.registerServiceWorker()).toBeNull();
	expect(register).not.toHaveBeenCalled();
	expect(await pwa.removeAppServiceWorkers()).toBe(2);
	expect(unregister).toHaveBeenCalledTimes(2);
});

it('a browser keeps its installed web app service worker', async () => {
	const { pwa } = await import('./pwa.svelte');
	expect(await pwa.removeAppServiceWorkers()).toBe(0);
	expect(unregister).not.toHaveBeenCalled();
});
