import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const m = vi.hoisted(() => ({ startSignIn: vi.fn() }));
vi.mock('$lib/api/plugin-oauth', () => ({ startSignIn: m.startSignIn }));
import {
	SignInFlow,
	accessText,
	clientLine,
	ownerClientFallback,
	pluginName,
	reasonText,
	returnAddressUsable,
	signInView,
	stateHint
} from './plugin-sign-in.svelte';

const words = (text: string) => text.split(/\s+/).filter((w) => w !== '·').length;

describe('signInView', () => {
	it.each([
		// state, client_configured, client_source, enabled → step, manual
		['signed_out', true, undefined, true, 'sign_in', 'available'],
		['signed_out', true, 'owner', true, 'sign_in', 'available'],
		['signed_out', false, 'plugin', true, 'sign_in', 'none'],
		['signed_out', false, 'registered', true, 'sign_in', 'none'],
		['signed_out', false, 'none', true, 'setup', 'needed'],
		['not_configured', false, undefined, true, 'setup', 'needed'],
		['not_configured', false, 'none', true, 'setup', 'needed'],
		['not_configured', false, 'plugin', true, 'setup', 'needed'],
		['signed_in', true, undefined, true, 'signed_in', 'available'],
		['signed_in', false, 'plugin', true, 'signed_in', 'none'],
		['signed_out', true, undefined, false, 'plugin_off', 'available'],
		['not_configured', false, undefined, false, 'setup', 'needed'],
		['invalid', false, undefined, true, 'invalid', 'none'],
		['invalid', true, 'owner', true, 'invalid', 'none']
	] as const)('%s with client %s / source %s, on=%s → %s, manual %s', (state, configured, source, enabled, step, manual) => {
		const view = signInView({ state, client_configured: configured, client_source: source }, enabled);
		expect(view.step).toBe(step);
		expect(view.manual).toBe(manual);
		expect(view.again).toBe(false);
	});

	it('follows client_source, not the declared fields', () => {
		const shipped = { client_configured: false, plugin_client_id: 'shipped' };
		const registrable = { client_configured: false, registration_url: 'https://auth.example.com/register' };
		// The worker reports a shipped or registrable client as `plugin` / `registered`.
		expect(signInView({ ...shipped, state: 'signed_out', client_source: 'plugin' }, true)).toEqual({ step: 'sign_in', again: false, manual: 'none' });
		expect(signInView({ ...registrable, state: 'signed_out', client_source: 'registered' }, true)).toEqual({ step: 'sign_in', again: false, manual: 'none' });
		// Registration refused: not_configured, reason registration_failed, source none.
		expect(signInView({ ...registrable, state: 'not_configured', client_source: 'none' }, true)).toEqual({ step: 'setup', again: false, manual: 'needed' });
		expect(stateHint({ step: 'setup', again: false, manual: 'needed' }, 'registration_failed')).toBe('Automatic setup failed');
		// An owner client wins over a shipped one and stays changeable.
		expect(signInView({ ...shipped, client_configured: true, state: 'signed_in', client_source: 'owner' }, true).manual).toBe('available');
	});

	it('names the client a sign-in uses', () => {
		const base = { state: 'signed_out', client_configured: false, client_id: '' } as const;
		expect(clientLine({ ...base, client_source: 'plugin', plugin_client_id: 'shipped-123' })).toEqual({ text: 'Uses the plugin’s client', code: 'shipped-123' });
		expect(clientLine({ ...base, client_source: 'registered', registration_url: 'https://auth.example.com/oauth/register' })).toEqual({ text: 'Set up automatically with', code: 'auth.example.com' });
		expect(clientLine({ ...base, client_configured: true, client_source: 'owner', client_id: 'mine' })).toEqual({ text: 'Uses your client', code: 'mine' });
		expect(clientLine({ ...base, client_configured: true, client_id: 'legacy' })).toEqual({ text: 'Uses your client', code: 'legacy' });
		expect(clientLine({ ...base, state: 'not_configured', client_source: 'none' })).toBeNull();
		expect(clientLine({ ...base, state: 'invalid', client_source: 'none' })).toBeNull();
		expect(ownerClientFallback({ plugin_client_id: 'shipped' })).toMatch(/plugin’s own client/);
		expect(ownerClientFallback({ registration_url: 'https://auth.example.com/register' })).toMatch(/automatically/);
		expect(ownerClientFallback({})).toMatch(/needs a new client/);
	});

	it('asks to sign in again with a client, or for setup without one', () => {
		expect(signInView({ state: 'needs_sign_in', client_configured: true }, true)).toEqual({ step: 'sign_in', again: true, manual: 'available' });
		expect(signInView({ state: 'needs_sign_in', client_configured: false, client_source: 'none' }, true)).toEqual({ step: 'setup', again: true, manual: 'needed' });
	});

	it('words reasons plainly and keeps settings hints to five words', () => {
		for (const reason of ['scopes_widened', 'endpoint_changed', 'client_changed', 'expired', 'unreadable', 'registration_failed', 'other']) {
			expect(reasonText(reason)).toMatch(/[.]$/);
			expect(words(reasonText(reason, true))).toBeLessThanOrEqual(5);
		}
		expect(reasonText('expired')).toBe('Your sign-in expired. Sign in again.');
		for (const step of ['sign_in', 'setup', 'signed_in', 'plugin_off', 'invalid'] as const) {
			expect(words(stateHint({ step, again: false, manual: 'none' }, ''))).toBeLessThanOrEqual(5);
		}
		expect(stateHint({ step: 'sign_in', again: true, manual: 'none' }, 'scopes_widened')).toBe('Asks for more access');
	});

	it('accepts only return addresses the worker accepts', () => {
		expect(returnAddressUsable('https://shuffle.example.com/api/v1/plugins/oauth/callback')).toBe(true);
		expect(returnAddressUsable('http://localhost:8000/api/v1/plugins/oauth/callback')).toBe(true);
		expect(returnAddressUsable('http://127.0.0.1:8000/x')).toBe(true);
		expect(returnAddressUsable('http://192.168.1.4:8000/x')).toBe(false);
		expect(returnAddressUsable('nonsense')).toBe(false);
	});

	it('names plugins and access', () => {
		expect(pluginName('dns-reader@my-marketplace')).toBe('dns-reader');
		expect(pluginName('plain')).toBe('plain');
		expect(accessText('write')).toBe('Read and write');
		expect(accessText('read')).toBe('Read-only');
		expect(accessText(undefined)).toBe('Read-only');
	});
});

describe('SignInFlow', () => {
	let popup: { location: { href: string }; close: ReturnType<typeof vi.fn>; closed: boolean };
	let open: ReturnType<typeof vi.fn>;
	const started = { authorization_url: 'https://auth.example.com/authorize?state=abc', flow_id: 'flow-1', redirect_uri: 'https://w/cb', expires_in: 600 };
	const post = (data: Record<string, unknown>, origin = window.location.origin) =>
		window.dispatchEvent(new MessageEvent('message', { data: { type: 'plugin-oauth', plugin_id: 'p@m', service: 'svc', state: null, error: null, ...data }, origin }));

	beforeEach(() => {
		popup = { location: { href: 'about:blank' }, close: vi.fn(), closed: false };
		open = vi.fn(() => popup);
		vi.stubGlobal('open', open);
		m.startSignIn.mockResolvedValue(started);
	});
	afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); vi.clearAllMocks(); });

	it('opens the window from the click, keeps the opener and navigates it to the provider', async () => {
		const flow = new SignInFlow('p@m', 'svc', vi.fn());
		const pending = flow.begin();
		expect(open).toHaveBeenCalledWith('about:blank', '_blank', expect.not.stringContaining('noopener'));
		expect(flow.phase).toBe('waiting');
		await pending;
		expect(m.startSignIn).toHaveBeenCalledWith('p@m', 'svc');
		expect(popup.location.href).toBe(started.authorization_url);
		flow.dispose();
	});

	it('reports a blocked window without calling the worker', async () => {
		open.mockReturnValue(null);
		const flow = new SignInFlow('p@m', 'svc', vi.fn());
		await flow.begin();
		expect(flow.phase).toBe('error');
		expect(flow.error).toMatch(/Allow pop-ups/);
		expect(m.startSignIn).not.toHaveBeenCalled();
	});

	it('shows the worker refusal and closes the window when start fails', async () => {
		m.startSignIn.mockRejectedValue({ status: 409, detail: 'Turn this plugin on in Settings > Plugins before signing in to its services.' });
		const flow = new SignInFlow('p@m', 'svc', vi.fn());
		await flow.begin();
		expect(flow.phase).toBe('error');
		expect(flow.error).toMatch(/Turn this plugin on/);
		expect(popup.close).toHaveBeenCalled();
	});

	it('re-reads status after a refused automatic setup so the manual setup can appear', async () => {
		m.startSignIn.mockRejectedValue({ status: 409, detail: 'The service did not accept automatic sign-in setup.' });
		const check = vi.fn().mockResolvedValue('not_configured');
		const flow = new SignInFlow('p@m', 'svc', check);
		await flow.begin();
		expect(flow.phase).toBe('error');
		await vi.waitFor(() => expect(check).toHaveBeenCalledOnce());
	});

	it('accepts only its own flow message from this origin, then re-reads status', async () => {
		const check = vi.fn().mockResolvedValue('signed_in');
		const flow = new SignInFlow('p@m', 'svc', check);
		await flow.begin();
		post({ flow_id: 'flow-1', state: 'signed_in' }, 'https://evil.example');
		post({ flow_id: 'other', state: 'signed_in' });
		expect(flow.phase).toBe('waiting');
		post({ flow_id: 'flow-1', state: 'signed_in' });
		expect(flow.phase).toBe('done');
		expect(check).toHaveBeenCalledOnce();
		expect(popup.close).toHaveBeenCalled();
	});

	it('shows the callback error, such as a cancelled sign-in', async () => {
		const flow = new SignInFlow('p@m', 'svc', vi.fn().mockResolvedValue('signed_out'));
		await flow.begin();
		post({ flow_id: 'flow-1', error: 'The sign-in was cancelled at the service. Nothing was changed.' });
		expect(flow.phase).toBe('error');
		expect(flow.error).toMatch(/cancelled/);
	});

	it('re-reads status on focus when no message arrives, and keeps listening until expiry', async () => {
		vi.useFakeTimers();
		const check = vi.fn().mockResolvedValueOnce('signed_out').mockResolvedValueOnce('signed_in');
		const flow = new SignInFlow('p@m', 'svc', check);
		await flow.begin();
		window.dispatchEvent(new Event('focus'));
		await vi.waitFor(() => expect(flow.unfinished).toBe(true));
		expect(flow.phase).toBe('idle');
		window.dispatchEvent(new Event('focus'));
		await vi.waitFor(() => expect(flow.phase).toBe('done'));
		expect(flow.unfinished).toBe(false);
	});

	it('expires an unfinished flow and ignores a late message', async () => {
		vi.useFakeTimers();
		const check = vi.fn();
		const flow = new SignInFlow('p@m', 'svc', check);
		await flow.begin();
		await vi.advanceTimersByTimeAsync(600_000);
		expect(flow.phase).toBe('error');
		expect(flow.error).toMatch(/expired/);
		post({ flow_id: 'flow-1', state: 'signed_in' });
		expect(flow.phase).toBe('error');
		expect(check).not.toHaveBeenCalled();
	});

	it('cancel closes the window and stops listening', async () => {
		const check = vi.fn();
		const flow = new SignInFlow('p@m', 'svc', check);
		await flow.begin();
		flow.cancel();
		expect(flow.phase).toBe('idle');
		expect(popup.close).toHaveBeenCalled();
		post({ flow_id: 'flow-1', state: 'signed_in' });
		window.dispatchEvent(new Event('focus'));
		expect(check).not.toHaveBeenCalled();
	});
});
