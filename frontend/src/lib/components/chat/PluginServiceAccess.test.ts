import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
const m = vi.hoisted(() => ({ post: vi.fn(), delete: vi.fn(), getSignInStatus: vi.fn(), startSignIn: vi.fn(), saveClient: vi.fn(), mobile: false }));
vi.mock('$lib/api/client', () => ({
	api: { post: m.post, delete: m.delete },
	getErrorMessage: (e: { detail?: string }, fallback: string) => e?.detail || fallback
}));
vi.mock('$lib/api/plugin-oauth', () => ({ getSignInStatus: m.getSignInStatus, startSignIn: m.startSignIn, saveClient: m.saveClient }));
vi.mock('$lib/api/runtime', () => ({ isBundledMobile: () => m.mobile }));
import PluginServiceAccess from './PluginServiceAccess.svelte';
import PrivateAccessRequest from './PrivateAccessRequest.svelte';

const endpoint = '/chat-access/s1/requests/r1';
const request = { id: 'r1', kind: 'service', purpose: 'List DNS zones', expires_at: Date.now() / 1000 + 86400, suggested_url: 'https://api.cloudflare.com/client/v4', plugin: 'dns-reader@mp', service: 'cloudflare' };
function status(service: Record<string, unknown> = {}, extra: Record<string, unknown> = {}) {
	return {
		plugin_id: 'dns-reader@mp', enabled: true, redirect_uri: 'https://w.example/api/v1/plugins/oauth/callback',
		services: [{ plugin_id: 'dns-reader@mp', service: 'cloudflare', state: 'signed_out', reason: '', client_configured: true, client_id: 'cid',
			client_source: 'owner', plugin_client_id: '', registration_url: '',
			requested_scopes: ['zone:read', 'dns:read'], scopes: [], access: 'read', authorize_host: 'dash.cloudflare.com', ...service }],
		...extra
	};
}
function mount(props: Record<string, unknown> = {}) {
	const onComplete = vi.fn();
	return { onComplete, ...render(PluginServiceAccess, { sessionId: 's1', request, onComplete, ...props }) };
}
let popup: { location: { href: string }; close: ReturnType<typeof vi.fn> };

beforeEach(() => {
	vi.clearAllMocks();
	m.mobile = false;
	Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
	popup = { location: { href: 'about:blank' }, close: vi.fn() };
	vi.stubGlobal('open', vi.fn(() => popup));
	m.post.mockResolvedValue({ granted: true });
	m.delete.mockResolvedValue({});
	m.startSignIn.mockResolvedValue({ authorization_url: 'https://dash.cloudflare.com/oauth2/auth?x', flow_id: 'f1', redirect_uri: '', expires_in: 600 });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('plugin service sign-in card', () => {
	it('replaces the key form for plugin requests', async () => {
		m.getSignInStatus.mockResolvedValue(status());
		render(PrivateAccessRequest, { sessionId: 's1', request, savedConnections: [], vaultAvailable: true, onComplete: vi.fn() });
		expect(await screen.findByRole('button', { name: 'Sign in with dash.cloudflare.com' })).toBeTruthy();
		expect(screen.queryByLabelText('API key or token')).toBeNull();
		expect(screen.queryByText('Continue to setup')).toBeNull();
	});

	it('shows plugin, service, host and read-only access with scopes collapsed, and one sign-in button', async () => {
		m.getSignInStatus.mockResolvedValue(status());
		mount();
		expect(await screen.findByRole('button', { name: 'Sign in with dash.cloudflare.com' })).toBeTruthy();
		expect(m.getSignInStatus).toHaveBeenCalledWith('dns-reader@mp', 'cloudflare');
		expect(screen.getByText('dns-reader')).toBeTruthy();
		expect(screen.getByText('Read-only')).toBeTruthy();
		const scopes = screen.getByText('Scopes asked for').closest('details')!;
		expect(scopes.open).toBe(false);
		expect(scopes.textContent).toContain('zone:read');
		expect(screen.queryByText('Set up manually')).toBeNull();
		expect(screen.queryByRole('button', { name: 'Grant access' })).toBeNull();
		expect(screen.queryByRole('checkbox', { name: /Allow changes/ })).toBeNull();
	});

	it('signs in, then grants in the same flow with sign_in true and no secret', async () => {
		m.getSignInStatus.mockResolvedValueOnce(status()).mockResolvedValue(status({ state: 'signed_in', scopes: ['zone:read', 'dns:read'] }));
		const { onComplete } = mount();
		await fireEvent.click(await screen.findByRole('button', { name: 'Sign in with dash.cloudflare.com' }));
		expect(await screen.findByRole('button', { name: 'Waiting…' })).toBeTruthy();
		expect(screen.getByText(/in the window that opened/)).toBeTruthy();
		await waitFor(() => expect(popup.location.href).toContain('dash.cloudflare.com'));
		window.dispatchEvent(new MessageEvent('message', { origin: window.location.origin, data: { type: 'plugin-oauth', flow_id: 'f1', plugin_id: 'dns-reader@mp', service: 'cloudflare', state: 'signed_in', error: null } }));
		const grant = await screen.findByRole('button', { name: 'Grant access' });
		expect(screen.getByText('Signed in. Grant access to finish.')).toBeTruthy();
		await waitFor(() => expect(document.activeElement).toBe(grant));
		await fireEvent.click(grant);
		await waitFor(() => expect(onComplete).toHaveBeenCalledOnce());
		expect(m.post).toHaveBeenCalledWith(endpoint, { sign_in: true, writable: false, minutes: 60 });
	});

	it('offers Allow changes only when the plugin declares write access', async () => {
		m.getSignInStatus.mockResolvedValue(status({ state: 'signed_in', access: 'write' }));
		mount();
		await fireEvent.click(await screen.findByRole('checkbox', { name: /Allow changes/ }));
		expect(screen.getByText('Read and write')).toBeTruthy();
		await fireEvent.change(screen.getByLabelText('Access for'), { target: { value: '120' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Grant access' }));
		expect(m.post).toHaveBeenCalledWith(endpoint, { sign_in: true, writable: true, minutes: 120 });
	});

	it('explains a cancelled sign-in and lets the owner try again', async () => {
		m.getSignInStatus.mockResolvedValue(status());
		mount();
		await fireEvent.click(await screen.findByRole('button', { name: 'Sign in with dash.cloudflare.com' }));
		await waitFor(() => expect(m.startSignIn).toHaveBeenCalled());
		await waitFor(() => expect(popup.location.href).not.toBe('about:blank'));
		window.dispatchEvent(new MessageEvent('message', { origin: window.location.origin, data: { type: 'plugin-oauth', flow_id: 'f1', state: null, error: 'The sign-in was cancelled at the service. Nothing was changed.' } }));
		expect((await screen.findByRole('alert')).textContent).toMatch(/cancelled at the service/);
		expect(screen.getByRole('button', { name: 'Sign in with dash.cloudflare.com' })).toBeTruthy();
	});

	it('says why a sign-in is needed again', async () => {
		m.getSignInStatus.mockResolvedValue(status({ state: 'needs_sign_in', reason: 'scopes_widened' }));
		mount();
		expect(await screen.findByText(/now asks for more access/)).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Sign in with dash.cloudflare.com' })).toBeTruthy();
	});

	it('tells the owner to switch the plugin on, without a sign-in button', async () => {
		m.getSignInStatus.mockResolvedValue(status({}, { enabled: false }));
		mount();
		expect((await screen.findByRole('alert')).textContent).toMatch(/switched off/);
		expect(screen.queryByRole('button', { name: /Sign in with/ })).toBeNull();
		expect(screen.getByRole('button', { name: 'Check again' })).toBeTruthy();
	});

	it('tucks the manual client setup behind a disclosure when no client exists', async () => {
		m.getSignInStatus.mockResolvedValueOnce(status({ state: 'not_configured', client_configured: false, client_id: '' }))
			.mockResolvedValue(status());
		m.saveClient.mockResolvedValue(status().services[0]);
		mount();
		const summary = await screen.findByText('Set up manually');
		const details = summary.closest('details')!;
		expect(details.open).toBe(false);
		expect(screen.queryByRole('button', { name: /Sign in with/ })).toBeNull();
		await fireEvent.click(summary);
		expect(screen.getByText('https://w.example/api/v1/plugins/oauth/callback')).toBeTruthy();
		await fireEvent.input(screen.getByLabelText('Client ID'), { target: { value: 'my-client' } });
		await fireEvent.input(screen.getByLabelText(/Client secret/), { target: { value: 'test-only-secret' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Save client' }));
		expect(m.saveClient).toHaveBeenCalledWith('dns-reader@mp', 'cloudflare', 'my-client', 'test-only-secret');
		expect(await screen.findByRole('button', { name: 'Sign in with dash.cloudflare.com' })).toBeTruthy();
		expect(document.body.textContent).not.toContain('test-only-secret');
	});

	it('says when automatic setup failed and offers the manual setup', async () => {
		m.getSignInStatus.mockResolvedValue(status({ state: 'not_configured', reason: 'registration_failed', client_configured: false, client_id: '', client_source: 'none', registration_url: 'https://dash.cloudflare.com/register' }));
		mount();
		expect(await screen.findByText(/Automatic setup didn’t work/)).toBeTruthy();
		expect(screen.getByText('Set up manually')).toBeTruthy();
		expect(screen.queryByRole('button', { name: /Sign in with/ })).toBeNull();
	});

	it('uses a client from the plugin without any setup, and names it', async () => {
		m.getSignInStatus.mockResolvedValue(status({ client_configured: false, client_source: 'plugin', client_id: '', plugin_client_id: 'shipped-public-id' }));
		mount();
		expect(await screen.findByRole('button', { name: 'Sign in with dash.cloudflare.com' })).toBeTruthy();
		expect(screen.queryByText('Set up manually')).toBeNull();
		expect(screen.getByText('Uses the plugin’s client')).toBeTruthy();
		expect(screen.getByText('shipped-public-id')).toBeTruthy();
	});

	it('signs in with automatic setup, naming the registration host', async () => {
		m.getSignInStatus.mockResolvedValue(status({ client_configured: false, client_source: 'registered', client_id: '', registration_url: 'https://auth.cloudflare.com/oauth/register' }));
		mount();
		expect(await screen.findByRole('button', { name: 'Sign in with dash.cloudflare.com' })).toBeTruthy();
		expect(screen.getByText('Set up automatically with')).toBeTruthy();
		expect(screen.getByText('auth.cloudflare.com')).toBeTruthy();
		expect(screen.queryByText('Set up manually')).toBeNull();
	});

	it('falls back to the manual setup when the provider refuses automatic setup at sign-in', async () => {
		m.getSignInStatus.mockResolvedValueOnce(status({ client_configured: false, client_source: 'registered', client_id: '', registration_url: 'https://auth.cloudflare.com/oauth/register' }))
			.mockResolvedValue(status({ state: 'not_configured', reason: 'registration_failed', client_configured: false, client_source: 'none', client_id: '', registration_url: 'https://auth.cloudflare.com/oauth/register' }));
		m.startSignIn.mockRejectedValue({ status: 409, detail: 'The service did not accept automatic sign-in setup. Enter an OAuth client.' });
		mount();
		await fireEvent.click(await screen.findByRole('button', { name: 'Sign in with dash.cloudflare.com' }));
		expect(await screen.findByText(/Automatic setup didn’t work/)).toBeTruthy();
		expect(screen.getByText('Set up manually')).toBeTruthy();
		expect(screen.queryByRole('button', { name: /Sign in with/ })).toBeNull();
		expect(popup.close).toHaveBeenCalled();
	});

	it('refuses to start from an address the provider cannot return to', async () => {
		m.getSignInStatus.mockResolvedValue(status({}, { redirect_uri: 'http://192.168.1.5:8000/api/v1/plugins/oauth/callback' }));
		mount();
		expect((await screen.findByRole('alert')).textContent).toMatch(/HTTPS address/);
		expect(screen.queryByRole('button', { name: /Sign in with/ })).toBeNull();
	});

	it('points a mirrored conversation to the device that runs it and still allows the grant', async () => {
		mount({ owner: 'ServerWorker' });
		expect(await screen.findByText(/sign in to cloudflare from AI Shuffle on ServerWorker/)).toBeTruthy();
		expect(m.getSignInStatus).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Grant access' }));
		expect(m.post).toHaveBeenCalledWith(endpoint, { sign_in: true, writable: false, minutes: 60 });
	});

	it('does not offer the provider window inside the phone app', async () => {
		m.mobile = true;
		m.getSignInStatus.mockResolvedValue(status());
		mount();
		expect(await screen.findByText(/Sign in from a browser or the desktop app/)).toBeTruthy();
		expect(screen.queryByRole('button', { name: /Sign in with/ })).toBeNull();
	});

	it('shows the worker refusal when the grant is refused and re-reads the state', async () => {
		m.getSignInStatus.mockResolvedValue(status({ state: 'signed_in' }));
		m.post.mockRejectedValueOnce({ status: 400, detail: 'Sign in to cloudflare first, then grant access.' });
		mount();
		await fireEvent.click(await screen.findByRole('button', { name: 'Grant access' }));
		expect((await screen.findByRole('alert')).textContent).toMatch(/Sign in to cloudflare first/);
		await waitFor(() => expect(m.getSignInStatus).toHaveBeenCalledTimes(2));
	});

	it('handles an uninstalled plugin, a load failure and an invalid declaration', async () => {
		m.getSignInStatus.mockRejectedValueOnce({ status: 404 });
		mount();
		expect((await screen.findByRole('alert')).textContent).toMatch(/no longer installed/);
		cleanup();
		m.getSignInStatus.mockRejectedValueOnce({ status: 500, detail: 'Secure credential storage is locked on this worker.' }).mockResolvedValue(status());
		mount();
		expect((await screen.findByRole('alert')).textContent).toMatch(/locked/);
		await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
		expect(await screen.findByRole('button', { name: 'Sign in with dash.cloudflare.com' })).toBeTruthy();
		cleanup();
		m.getSignInStatus.mockResolvedValue(status({ state: 'invalid' }));
		mount();
		expect((await screen.findByRole('alert')).textContent).toMatch(/broken/);
		expect(screen.queryByRole('button', { name: /Sign in with|Grant access/ })).toBeNull();
	});

	describe('a plugin’s own client through the account hub', () => {
		const RELAY = 'https://hobbycoders.com/browser/plugin-oauth/callback';
		const shipped = (relay: Record<string, unknown> = {}) => status(
			{ client_configured: false, client_source: 'plugin', client_id: '', plugin_client_id: 'shipped-public-id', return_address: RELAY, relay: 'used', relay_reason: '', ...relay },
			{ relay_uri: RELAY, relay_available: relay.relay === undefined || relay.relay === 'used' }
		);
		const unavailable = (relay_reason: string) => shipped({ relay: 'unavailable', relay_reason, return_address: 'https://w.example/api/v1/plugins/oauth/callback' });

		it('signs in directly, says it returns through hobbycoders.com and asks to confirm there', async () => {
			m.getSignInStatus.mockResolvedValue(shipped());
			let answer: (v: unknown) => void = () => {};
			m.startSignIn.mockReturnValue(new Promise((r) => { answer = r; }));
			mount();
			const button = await screen.findByRole('button', { name: 'Sign in with dash.cloudflare.com' });
			expect(screen.getByText('Returns through hobbycoders.com')).toBeTruthy();
			expect(screen.getByText('Uses the plugin’s client')).toBeTruthy();
			expect(screen.queryByText('Set up manually')).toBeNull();
			await fireEvent.click(button);
			// Before the worker answers, the status already says the sign-in is relayed.
			expect(await screen.findByText(/AI Shuffle asks you to confirm once on hobbycoders\.com/)).toBeTruthy();
			answer({ authorization_url: 'https://dash.cloudflare.com/oauth2/auth?x', flow_id: 'f1', redirect_uri: RELAY, expires_in: 600, relayed: true, return_origin: 'https://w.example' });
			await waitFor(() => expect(popup.location.href).toContain('dash.cloudflare.com'));
			expect(screen.getByText(/^Finish in the sign-in window\. AI Shuffle asks you to confirm once on hobbycoders\.com\./)).toBeTruthy();
			expect(screen.getByRole('button', { name: 'Stop waiting' })).toBeTruthy();
			// The callback page still posts from the worker's own origin.
			m.getSignInStatus.mockResolvedValue(shipped({ state: 'signed_in' }));
			window.dispatchEvent(new MessageEvent('message', { origin: window.location.origin, data: { type: 'plugin-oauth', flow_id: 'f1', state: 'signed_in', error: null } }));
			expect(await screen.findByRole('button', { name: 'Grant access' })).toBeTruthy();
		});

		it('offers the manual setup with the connect-this-worker line on an unlinked worker', async () => {
			m.getSignInStatus.mockResolvedValue(unavailable('not_linked'));
			mount();
			expect(await screen.findByText('To use dns-reader’s app without setup, connect this worker to your AI Shuffle account in Settings › Account.')).toBeTruthy();
			const details = screen.getByText('Set up manually').closest('details')!;
			expect(details.open).toBe(false);
			expect(screen.queryByRole('button', { name: /Sign in with/ })).toBeNull();
			expect(screen.queryByText('Uses the plugin’s client')).toBeNull();
			expect(screen.queryByText(/Returns through/)).toBeNull();
			await fireEvent.click(screen.getByText('Set up manually'));
			// The worker's own address, never the hub's.
			expect(screen.getByText('https://w.example/api/v1/plugins/oauth/callback')).toBeTruthy();
			expect(screen.getByText('Add this to your own app at the provider')).toBeTruthy();
		});

		it('keeps the existing message for a plain-HTTP address, with the manual setup', async () => {
			m.getSignInStatus.mockResolvedValue(unavailable('insecure_address'));
			mount();
			expect((await screen.findByRole('alert')).textContent).toBe('Signing in needs this worker’s HTTPS address or a browser on this computer.');
			expect(screen.getByText('Set up manually')).toBeTruthy();
			expect(screen.queryByRole('button', { name: /Sign in with/ })).toBeNull();
		});

		it('explains an account service that isn’t set up on this worker', async () => {
			m.getSignInStatus.mockResolvedValue(unavailable('not_configured'));
			mount();
			expect(await screen.findByText(/dns-reader’s app can’t be used on this worker/)).toBeTruthy();
			expect(screen.getByText('Set up manually')).toBeTruthy();
		});

		it('keeps an existing sign-in usable on an unlinked worker', async () => {
			m.getSignInStatus.mockResolvedValue(shipped({ state: 'signed_in', relay: 'unavailable', relay_reason: 'not_linked' }));
			mount();
			expect(await screen.findByRole('button', { name: 'Grant access' })).toBeTruthy();
			expect(screen.queryByText('Set up manually')).toBeNull();
		});

		it('re-reads the status after a 409 at start and switches to the manual setup', async () => {
			m.getSignInStatus.mockResolvedValueOnce(shipped()).mockResolvedValue(unavailable('not_linked'));
			m.startSignIn.mockRejectedValue({ status: 409, detail: 'Signing in with this plugin\'s own app needs this worker connected to your AI Shuffle account (Settings > Account). You can also enter your own OAuth client.' });
			mount();
			await fireEvent.click(await screen.findByRole('button', { name: 'Sign in with dash.cloudflare.com' }));
			expect(await screen.findByText(/connect this worker to your AI Shuffle account in Settings › Account/)).toBeTruthy();
			expect(screen.getByText('Set up manually')).toBeTruthy();
			expect(screen.queryByRole('button', { name: /Sign in with/ })).toBeNull();
			expect(popup.close).toHaveBeenCalled();
			expect(m.getSignInStatus).toHaveBeenCalledTimes(2);
		});

		it('shows the account service failure and offers the manual setup next to trying again', async () => {
			m.getSignInStatus.mockResolvedValue(shipped());
			m.startSignIn.mockRejectedValue({ status: 502, detail: 'The AI Shuffle account service could not be reached or needs an update. Try again shortly.' });
			mount();
			await fireEvent.click(await screen.findByRole('button', { name: 'Sign in with dash.cloudflare.com' }));
			expect((await screen.findByRole('alert')).textContent).toMatch(/needs an update/);
			expect(screen.getByText('Set up manually').closest('details')!.open).toBe(false);
			expect(screen.getByRole('button', { name: 'Sign in with dash.cloudflare.com' })).toBeTruthy();
			await fireEvent.click(screen.getByText('Set up manually'));
			expect(screen.getByText('Add this to your own app at the provider')).toBeTruthy();
		});

		it('does not offer the manual setup when too many sign-ins wait', async () => {
			m.getSignInStatus.mockResolvedValue(shipped());
			m.startSignIn.mockRejectedValue({ status: 429, detail: 'Too many sign-ins are waiting. Try again in a few minutes.' });
			mount();
			await fireEvent.click(await screen.findByRole('button', { name: 'Sign in with dash.cloudflare.com' }));
			expect((await screen.findByRole('alert')).textContent).toMatch(/Too many sign-ins/);
			expect(screen.queryByText('Set up manually')).toBeNull();
		});

		it('behaves as before on a worker without the relay fields', async () => {
			m.getSignInStatus.mockResolvedValue(status({ client_configured: false, client_source: 'plugin', client_id: '', plugin_client_id: 'shipped-public-id' }));
			mount();
			await fireEvent.click(await screen.findByRole('button', { name: 'Sign in with dash.cloudflare.com' }));
			expect(await screen.findByText('Finish signing in to dash.cloudflare.com in the window that opened.', { exact: false })).toBeTruthy();
			expect(screen.queryByText(/Returns through|confirm once/)).toBeNull();
		});
	});

	it('cancels the request', async () => {
		m.getSignInStatus.mockResolvedValue(status());
		const { onComplete } = mount();
		await fireEvent.click(await screen.findByRole('button', { name: 'Cancel request' }));
		await waitFor(() => expect(onComplete).toHaveBeenCalledOnce());
		expect(m.delete).toHaveBeenCalledWith(endpoint);
	});
});
