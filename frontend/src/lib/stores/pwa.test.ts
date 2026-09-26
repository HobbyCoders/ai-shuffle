import { afterEach, beforeEach, expect, it, vi } from 'vitest';

vi.mock('$lib/services/desktop', () => ({ isDesktop: false }));
vi.mock('$lib/api/runtime', () => ({ isBundledMobile: () => false }));

const register = vi.fn(async () => ({ addEventListener() {}, installing: null, waiting: null, active: null, update: async () => {} }));
const baseAgent = navigator.userAgent;

function setUserAgent(value: string) {
	Object.defineProperty(navigator, 'userAgent', { configurable: true, get: () => value });
}

beforeEach(() => {
	register.mockClear();
	Object.defineProperty(navigator, 'serviceWorker', {
		configurable: true,
		value: { register, addEventListener() {}, removeEventListener() {}, controller: null }
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
