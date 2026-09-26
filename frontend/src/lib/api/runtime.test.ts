import { afterEach, beforeEach, expect, it, vi } from 'vitest';
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
