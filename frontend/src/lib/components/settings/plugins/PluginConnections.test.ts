import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
const m = vi.hoisted(() => ({
	getSignInStatus: vi.fn(), startSignIn: vi.fn(), signOut: vi.fn(), saveClient: vi.fn(), deleteClient: vi.fn(), mobile: false
}));
vi.mock('$lib/api/client', () => ({ getErrorMessage: (e: { detail?: string }, fallback: string) => e?.detail || fallback }));
vi.mock('$lib/api/plugin-oauth', () => ({ ...m }));
vi.mock('$lib/api/runtime', () => ({ isBundledMobile: () => m.mobile }));
import PluginConnections from './PluginConnections.svelte';

type Svc = Record<string, unknown>;
function status(services: Svc[], extra: Record<string, unknown> = {}) {
	return {
		plugin_id: 'dns@mp', enabled: true, redirect_uri: 'https://w.example/api/v1/plugins/oauth/callback',
		services: services.map((s) => ({ plugin_id: 'dns@mp', service: 'cloudflare', state: 'signed_out', reason: '', client_configured: true,
			client_id: 'cid-123', client_source: 'owner', plugin_client_id: '', registration_url: '', access: 'read', authorize_host: 'dash.cloudflare.com', ...s })),
		...extra
	};
}
const row = (name = 'cloudflare') => screen.findByTestId(`plugin-service-${name}`);
function mount(services = ['cloudflare'], enabled = true) {
	return render(PluginConnections, { pluginId: 'dns@mp', services, enabled });
}
let popup: { location: { href: string }; close: ReturnType<typeof vi.fn> };

beforeEach(() => {
	vi.clearAllMocks();
	m.mobile = false;
	Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
	popup = { location: { href: 'about:blank' }, close: vi.fn() };
	vi.stubGlobal('open', vi.fn(() => popup));
	m.startSignIn.mockResolvedValue({ authorization_url: 'https://dash.cloudflare.com/oauth2/auth', flow_id: 'f1', redirect_uri: '', expires_in: 600 });
	m.signOut.mockResolvedValue({ signed_out: true, revoked: true });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Settings > Plugins > Connections', () => {
	it('signs in with one button and shows the state with the provider host', async () => {
		m.getSignInStatus.mockResolvedValueOnce(status([{}])).mockResolvedValue(status([{ state: 'signed_in' }]));
		mount();
		const r = await row();
		expect(within(r).getByText('Not signed in · dash.cloudflare.com')).toBeTruthy();
		expect(within(r).getByText('Read-only')).toBeTruthy();
		await fireEvent.click(within(r).getByRole('button', { name: 'Sign in with dash.cloudflare.com' }));
		expect(await within(r).findByText('Waiting for sign-in')).toBeTruthy();
		await waitFor(() => expect(popup.location.href).toContain('dash.cloudflare.com'));
		window.dispatchEvent(new MessageEvent('message', { origin: window.location.origin, data: { type: 'plugin-oauth', flow_id: 'f1', state: 'signed_in', error: null } }));
		expect(await within(await row()).findByText('Signed in · dash.cloudflare.com')).toBeTruthy();
	});

	it('confirms sign-out inline, without a dialog', async () => {
		m.getSignInStatus.mockResolvedValueOnce(status([{ state: 'signed_in' }])).mockResolvedValue(status([{}]));
		mount();
		const r = await row();
		await fireEvent.click(within(r).getByRole('button', { name: 'Sign out' }));
		expect(document.querySelector('dialog')).toBeNull();
		expect(within(r).getByText('Sign out of cloudflare?')).toBeTruthy();
		expect(m.signOut).not.toHaveBeenCalled();
		const confirm = within(r).getAllByRole('button', { name: 'Sign out' }).at(-1)!;
		await fireEvent.click(confirm);
		await waitFor(() => expect(m.signOut).toHaveBeenCalledWith('dns@mp', 'cloudflare'));
		expect(await within(await row()).findByText('Not signed in · dash.cloudflare.com')).toBeTruthy();
	});

	it('offers manual setup only when no client is available, and saves the client', async () => {
		m.getSignInStatus.mockResolvedValueOnce(status([{ state: 'not_configured', client_configured: false, client_id: '' }]))
			.mockResolvedValue(status([{}]));
		m.saveClient.mockResolvedValue({});
		mount();
		const r = await row();
		expect(within(r).getByText('Needs one-time setup')).toBeTruthy();
		expect(within(r).queryByRole('button', { name: /Sign in/ })).toBeNull();
		const toggle = within(r).getByRole('button', { name: 'Set up manually' });
		expect(toggle.getAttribute('aria-expanded')).toBe('false');
		expect(screen.queryByLabelText('Client ID')).toBeNull();
		await fireEvent.click(toggle);
		expect(screen.getByText('https://w.example/api/v1/plugins/oauth/callback')).toBeTruthy();
		expect(screen.getByText('Add this to the provider')).toBeTruthy();
		await fireEvent.input(screen.getByLabelText('Client ID'), { target: { value: 'new-client' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Save client' }));
		await waitFor(() => expect(m.saveClient).toHaveBeenCalledWith('dns@mp', 'cloudflare', 'new-client', ''));
		expect(await within(await row()).findByRole('button', { name: 'Sign in with dash.cloudflare.com' })).toBeTruthy();
		expect(screen.queryByLabelText('Client ID')).toBeNull();
	});

	it.each([
		[{ client_source: 'plugin', plugin_client_id: 'shipped' }],
		[{ client_source: 'registered', registration_url: 'https://dash.cloudflare.com/oauth/register' }]
	])('hides manual setup when the plugin or a registration supplies the client (%o)', async (source) => {
		m.getSignInStatus.mockResolvedValue(status([{ client_configured: false, client_id: '', ...source }]));
		mount();
		const r = await row();
		expect(within(r).getByRole('button', { name: 'Sign in with dash.cloudflare.com' })).toBeTruthy();
		expect(within(r).queryByRole('button', { name: /Set up manually|Change client/ })).toBeNull();
	});

	it('offers the manual setup once the provider refuses automatic setup', async () => {
		const registered = { client_configured: false, client_id: '', registration_url: 'https://dash.cloudflare.com/oauth/register' };
		m.getSignInStatus.mockResolvedValueOnce(status([{ ...registered, client_source: 'registered' }]))
			.mockResolvedValue(status([{ ...registered, state: 'not_configured', reason: 'registration_failed', client_source: 'none' }]));
		m.startSignIn.mockRejectedValue({ status: 409, detail: 'The service did not accept automatic sign-in setup. Enter an OAuth client.' });
		mount();
		await fireEvent.click(within(await row()).getByRole('button', { name: 'Sign in with dash.cloudflare.com' }));
		const r = await row();
		expect(await within(r).findByText('Automatic setup failed')).toBeTruthy();
		expect(within(r).getByText(/did not accept automatic sign-in setup/)).toBeTruthy();
		expect(within(r).getByRole('button', { name: 'Set up manually' })).toBeTruthy();
		expect(within(r).queryByRole('button', { name: /Sign in/ })).toBeNull();
	});

	it('says what removing the owner’s client falls back to', async () => {
		m.getSignInStatus.mockResolvedValue(status([{ state: 'needs_sign_in', reason: 'client_changed', plugin_client_id: 'shipped' }]));
		mount();
		await fireEvent.click(within(await row()).getByRole('button', { name: 'Change client' }));
		expect(screen.getByText('Without it, sign-in uses the plugin’s own client.')).toBeTruthy();
	});

	it('keeps the owner’s own client out of the way until a new sign-in is needed', async () => {
		m.getSignInStatus.mockResolvedValue(status([{}]));
		mount();
		const r = await row();
		expect(within(r).getByRole('button', { name: 'Sign in with dash.cloudflare.com' })).toBeTruthy();
		expect(within(r).queryByRole('button', { name: /Set up manually|Change client/ })).toBeNull();
	});

	it('lets the owner replace or remove their own client when it stopped working', async () => {
		m.getSignInStatus.mockResolvedValueOnce(status([{ state: 'needs_sign_in', reason: 'client_changed' }]))
			.mockResolvedValue(status([{ state: 'not_configured', client_configured: false, client_source: 'none', client_id: '' }]));
		m.deleteClient.mockResolvedValue(undefined);
		mount();
		const r = await row();
		await fireEvent.click(within(r).getByRole('button', { name: 'Change client' }));
		expect(screen.getByText('Replaces the saved client')).toBeTruthy();
		expect(screen.getByText('cid-123')).toBeTruthy();
		expect(screen.getByText('Without it, signing in needs a new client.')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
		await fireEvent.click(screen.getAllByRole('button', { name: 'Remove' }).at(-1)!);
		await waitFor(() => expect(m.deleteClient).toHaveBeenCalledWith('dns@mp', 'cloudflare'));
		expect(await within(await row()).findByText('Needs one-time setup')).toBeTruthy();
	});

	it.each([
		['expired', 'Sign-in expired'],
		['scopes_widened', 'Asks for more access'],
		['client_changed', 'Sign-in client changed']
	])('asks to sign in again when %s', async (reason, hint) => {
		m.getSignInStatus.mockResolvedValue(status([{ state: 'needs_sign_in', reason }]));
		mount();
		const r = await row();
		expect(within(r).getByText(hint)).toBeTruthy();
		expect(within(r).getByRole('button', { name: 'Sign in with dash.cloudflare.com' }).textContent).toBe('Sign in again');
	});

	it('blocks sign-in while the plugin is off, and for an invalid declaration', async () => {
		m.getSignInStatus.mockResolvedValue(status([{}, { service: 'github', state: 'invalid', authorize_host: '' }], { enabled: false }));
		mount(['cloudflare', 'github'], false);
		const r = await row();
		expect(within(r).getByText('Turn the plugin on')).toBeTruthy();
		expect(within(r).queryByRole('button', { name: /Sign in/ })).toBeNull();
		const g = await row('github');
		expect(within(g).getByText('Sign-in declaration is invalid')).toBeTruthy();
		expect(within(g).queryAllByRole('button')).toHaveLength(0);
	});

	it('reloads when the plugin is switched on', async () => {
		m.getSignInStatus.mockResolvedValueOnce(status([{}], { enabled: false })).mockResolvedValue(status([{}]));
		const view = mount(['cloudflare'], false);
		expect(within(await row()).getByText('Turn the plugin on')).toBeTruthy();
		await view.rerender({ enabled: true });
		expect(await within(await row()).findByRole('button', { name: 'Sign in with dash.cloudflare.com' })).toBeTruthy();
	});

	it('shows a start refusal under the row', async () => {
		m.getSignInStatus.mockResolvedValue(status([{}]));
		m.startSignIn.mockRejectedValue({ status: 400, detail: 'The sign-in return address must be an https:// address (or http on this computer).' });
		mount();
		await fireEvent.click(within(await row()).getByRole('button', { name: 'Sign in with dash.cloudflare.com' }));
		expect((await screen.findByRole('alert')).textContent).toMatch(/return address/);
	});

	it('disables sign-in from a plain http address and inside the phone app', async () => {
		m.getSignInStatus.mockResolvedValue(status([{}], { redirect_uri: 'http://10.0.0.5:8000/api/v1/plugins/oauth/callback' }));
		mount();
		const r = await row();
		expect((within(r).getByRole('button', { name: 'Sign in with dash.cloudflare.com' }) as HTMLButtonElement).disabled).toBe(true);
		expect(screen.getByText(/needs this worker’s HTTPS address/)).toBeTruthy();
		cleanup();
		m.mobile = true;
		m.getSignInStatus.mockResolvedValue(status([{}]));
		mount();
		expect((within(await row()).getByRole('button', { name: 'Sign in with dash.cloudflare.com' }) as HTMLButtonElement).disabled).toBe(true);
		expect(screen.getByText('Sign in from a browser or the desktop app.')).toBeTruthy();
	});

	it('keeps services without a sign-in listed, and survives a status failure', async () => {
		m.getSignInStatus.mockRejectedValueOnce({ status: 500, detail: 'boom' }).mockResolvedValue(status([{}]));
		mount(['cloudflare', 'slack']);
		expect(await screen.findByText('Sign-in status unavailable')).toBeTruthy();
		expect(screen.getByText('slack')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
		expect(await row()).toBeTruthy();
		expect(screen.getByText('Asks you for access')).toBeTruthy();
	});
});
