import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
let withAuth: typeof import('./runtime').withAuth;
beforeEach(async () => {
	vi.resetModules();
	vi.stubGlobal('window', { addEventListener: vi.fn(), location: { origin: 'https://appassets.androidplatform.net' } });
	({ withAuth } = await import('./runtime'));
});
afterEach(() => vi.unstubAllGlobals());
it('keeps same-origin cookie auth but refuses redirects', () => {
	expect(withAuth({ method: 'POST' })).toEqual({ method: 'POST', credentials: 'include', redirect: 'error' });
});
it('a caller cannot opt an authenticated request back into following redirects', () => {
	expect(withAuth({ redirect: 'follow', credentials: 'omit' })).toEqual({ credentials: 'include', redirect: 'error' });
});
it.each([false, true])('keeps bundled bearer and Access cookie policy (cookies=%s) while refusing redirects', (sendCookies) => {
	window.__SHUFFLE_MOBILE__ = { apiOrigin: 'https://worker.example', token: 'shc_test', sendCookies };
	const init = withAuth({ headers: { 'Content-Type': 'application/json' }, redirect: 'follow' });
	expect(init.redirect).toBe('error');
	expect(init.credentials).toBe(sendCookies ? 'include' : 'omit');
	const headers = new Headers(init.headers);
	expect(headers.get('Authorization')).toBe('Bearer shc_test');
	expect(headers.get('Content-Type')).toBe('application/json');
});
it('also refuses redirects when called without a browser', () => {
	vi.stubGlobal('window', undefined);
	expect(withAuth().redirect).toBe('error');
});

describe('native capability descriptor', () => {
	const runtime = { apiOrigin: 'https://worker.example', token: 'shc_test' };
	let native: typeof import('./runtime');
	beforeEach(async () => {
		vi.resetModules();
		vi.stubGlobal('window', { addEventListener: vi.fn(), location: { origin: 'https://worker.example' } });
		native = await import('./runtime');
	});

	it('reports what the app offers', () => {
		window.__SHUFFLE_MOBILE__ = { ...runtime, native: { platform: 'ios', bridge: 1, capabilities: ['file-chooser', 'microphone'] } };
		expect(native.nativeBridge()).toEqual({ platform: 'ios', bridge: 1, capabilities: ['file-chooser', 'microphone'] });
		expect(native.nativeCapability('microphone')).toBe(true);
		expect(native.nativeCapability('file-save')).toBe(false);
	});

	it('an app from before the descriptor offers nothing', () => {
		window.__SHUFFLE_MOBILE__ = { ...runtime };
		expect(native.isBundledMobile()).toBe(true);
		expect(native.nativeBridge()).toBeNull();
		expect(native.nativeCapability('file-chooser')).toBe(false);
	});

	it('the web UI offers nothing', () => {
		expect(native.isBundledMobile()).toBe(false);
		expect(native.nativeCapability('microphone')).toBe(false);
	});

	it.each([
		{ bridge: 0, capabilities: ['microphone'] },
		{ bridge: 1.5, capabilities: ['microphone'] },
		{ bridge: 1, capabilities: 'microphone' }
	])('ignores a malformed descriptor %j', (descriptor) => {
		window.__SHUFFLE_MOBILE__ = { ...runtime, native: descriptor as never };
		expect(native.nativeBridge()).toBeNull();
		expect(native.nativeCapability('microphone')).toBe(false);
	});

	it('drops capability names that are not strings', () => {
		window.__SHUFFLE_MOBILE__ = { ...runtime, native: { bridge: 2, capabilities: ['microphone', 7 as never] } };
		expect(native.nativeBridge()?.capabilities).toEqual(['microphone']);
	});
});
