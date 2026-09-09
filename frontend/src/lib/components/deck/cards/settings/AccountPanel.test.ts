import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { tick } from 'svelte';

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }));
const authState = vi.hoisted(() => ({ current: { status: { auth_mode: 'local' } as { auth_mode?: string } }, logout: vi.fn() }));
vi.mock('$lib/stores/auth.svelte', () => ({ auth: authState }));
vi.mock('$lib/api/client', () => ({ api, getErrorMessage: (e: Error) => e.message }));
import AccountPanel from './AccountPanel.svelte';

const disconnected = { signed_in: false, status: 'disconnected', code: null, qr_svg: null, error: null };
const waiting = { ...disconnected, status: 'waiting', code: 'TESTONLYCODE', authorization_url: 'https://account.example.com/claim?code=TESTONLYCODE', signup_url: 'https://account.example.com/claim?code=TESTONLYCODE&screen=signup', qr_svg: '<svg></svg>' };
const profile = { account: { id: 'acct', username: 'desk.owner', profile_complete: true }, servers: [{ id: 's1', name: 'Desk', url: null, current: true }, { id: 's2', name: 'Laptop', url: 'https://laptop.example', current: false }] };
function reads(path: string, connection: unknown, account: unknown = profile) {
	api.get.mockImplementation(async (p: string) => {
		if (p === '/mobile/hub/connection') return connection;
		if (p === '/mobile/hub/account') return account;
		throw new Error('Unexpected GET ' + p);
	});
	void path;
}
beforeEach(() => { vi.resetAllMocks(); authState.current.status = { auth_mode: 'local' }; reads('', disconnected); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

it('offers sign in and account creation before anything is linked, then opens the matching page', async () => {
	api.post.mockResolvedValue(waiting);
	render(AccountPanel, { workerName: 'Desk worker' });
	expect(await screen.findByText('Not signed in')).toBeTruthy();
	expect(screen.queryByRole('link')).toBeNull();
	await fireEvent.click(screen.getByRole('button', { name: 'Create account' }));
	expect(api.post).toHaveBeenCalledExactlyOnceWith('/mobile/hub/connection', { name: 'Desk worker' });
	const link = await screen.findByRole('link', { name: 'Create account in browser' });
	expect(link.getAttribute('href')).toBe(waiting.signup_url);
	expect(screen.queryByText(/TEST ONLY CODE/)).toBeNull();
	await fireEvent.click(screen.getByRole('button', { name: 'Use my phone instead' }));
	expect(screen.getByRole('button', { name: 'Copy connection code' })).toBeTruthy();
	expect(screen.getByLabelText('Worker connection QR code')).toBeTruthy();
	api.delete.mockResolvedValue(disconnected);
	await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
	expect(api.delete).toHaveBeenCalledWith('/mobile/hub/connection');
	expect(screen.queryByRole('link')).toBeNull();
});

it('reopens an existing offer for sign in without minting a second code', async () => {
	reads('', waiting);
	render(AccountPanel);
	await fireEvent.click(await screen.findByRole('button', { name: 'Sign in' }));
	expect(api.post).not.toHaveBeenCalled();
	expect((await screen.findByRole('link', { name: 'Sign in in browser' })).getAttribute('href')).toBe(waiting.authorization_url);
});

it.each(['locked', 'unavailable'])('disables both entry points when the link is %s and explains why', async (status) => {
	reads('', { ...disconnected, status, error: 'Connection prerequisite unavailable' });
	render(AccountPanel);
	const signIn = await screen.findByRole('button', { name: 'Sign in' });
	expect((signIn as HTMLButtonElement).disabled).toBe(true);
	expect((screen.getByRole('button', { name: 'Create account' }) as HTMLButtonElement).disabled).toBe(true);
	expect(screen.getByRole('alert').textContent).toBe('Connection prerequisite unavailable');
	expect(signIn.getAttribute('aria-describedby')).toBe('account-panel-error');
});

it('shows the username, every worker on the account and signs out', async () => {
	reads('', { ...disconnected, signed_in: true, status: 'connected' });
	api.delete.mockResolvedValue(disconnected);
	render(AccountPanel);
	expect(await screen.findByText('@desk.owner')).toBeTruthy();
	const workers = screen.getByRole('list', { name: 'Workers on this account' });
	expect(within(workers).getByText('Laptop')).toBeTruthy();
	expect(within(workers).getByText('Desk (this worker)')).toBeTruthy();
	expect(screen.getByText('Remote access is on')).toBeTruthy();
	expect(screen.queryByRole('button', { name: 'Sign in' })).toBeNull();
	expect(screen.queryByRole('button', { name: 'Sign this browser out' })).toBeNull();
	await fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
	expect(api.delete).toHaveBeenCalledExactlyOnceWith('/mobile/hub/connection');
	expect(await screen.findByRole('button', { name: 'Sign in' })).toBeTruthy();
	expect(screen.queryByText('@desk.owner')).toBeNull();
});

it('asks for a username when the account still has a temporary one and saves it', async () => {
	reads('', { ...disconnected, signed_in: true, status: 'connected' }, { ...profile, account: { ...profile.account, username: 'user-12345', profile_complete: false } });
	api.patch.mockResolvedValue({ ...profile, account: { id: 'acct', username: 'chosen.name', profile_complete: true } });
	render(AccountPanel);
	const input = await screen.findByLabelText('Choose your username') as HTMLInputElement;
	expect(input.value).toBe('user-12345');
	await fireEvent.input(input, { target: { value: 'Bad Name!' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
	expect(api.patch).not.toHaveBeenCalled();
	expect(screen.getByRole('alert').textContent).toContain('3–64 letters');
	await fireEvent.input(input, { target: { value: ' Chosen.Name ' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
	expect(api.patch).toHaveBeenCalledExactlyOnceWith('/mobile/hub/account', { username: 'chosen.name' });
	expect(await screen.findByText('@chosen.name')).toBeTruthy();
	expect(screen.queryByLabelText('Choose your username')).toBeNull();
	await fireEvent.click(screen.getByRole('button', { name: 'Edit username' }));
	expect((screen.getByLabelText('Username') as HTMLInputElement).value).toBe('chosen.name');
	await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
	expect(screen.queryByLabelText('Username')).toBeNull();
});

it('keeps the profile error scoped and retryable, and offers browser sign-out for account sessions', async () => {
	authState.current.status = { auth_mode: 'account' };
	api.get.mockImplementation(async (p: string) => {
		if (p === '/mobile/hub/connection') return { ...disconnected, signed_in: true, status: 'connecting' };
		throw new Error('Profile unavailable');
	});
	render(AccountPanel);
	expect((await screen.findByRole('alert')).textContent).toContain('Profile unavailable');
	expect(screen.getByText('Turning on remote access…')).toBeTruthy();
	reads('', { ...disconnected, signed_in: true, status: 'connecting' });
	await fireEvent.click(screen.getByRole('button', { name: 'Retry profile' }));
	expect(await screen.findByText('@desk.owner')).toBeTruthy();
	await fireEvent.click(screen.getByRole('button', { name: 'Sign this browser out' }));
	expect(authState.logout).toHaveBeenCalledOnce();
});

it('reports a failed start and lets the user try again', async () => {
	api.post.mockRejectedValue(new Error('Account service is unavailable'));
	render(AccountPanel);
	await fireEvent.click(await screen.findByRole('button', { name: 'Sign in' }));
	expect((await screen.findByRole('alert')).textContent).toContain('Account service is unavailable');
	expect((screen.getByRole('button', { name: 'Sign in' }) as HTMLButtonElement).disabled).toBe(false);
});

it('keeps a local sign-out warning visible across background refreshes', async () => {
	vi.useFakeTimers();
	reads('', { ...disconnected, signed_in: true, status: 'connected' });
	api.delete.mockResolvedValue({ ...disconnected, error: 'Signed out on this worker. Remove its listing from your account.' });
	render(AccountPanel);
	await tick(); await vi.advanceTimersByTimeAsync(0);
	await fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
	reads('', disconnected);
	await vi.advanceTimersByTimeAsync(4000);
	expect(screen.getByRole('alert').textContent).toContain('Signed out on this worker.');
	expect(screen.getByRole('button', { name: 'Sign in' })).toBeTruthy();
});

it('does not guess the account state before the first answer arrives', async () => {
	let reject!: (reason: Error) => void;
	api.get.mockImplementation(() => new Promise((_, fail) => { reject = fail; }));
	render(AccountPanel);
	expect(screen.getByText('Checking account…')).toBeTruthy();
	expect(screen.queryByRole('button', { name: 'Sign in' })).toBeNull();
	reject(new Error('Account request failed'));
	await screen.findByRole('alert');
	expect(screen.getByText('Account status unavailable')).toBeTruthy();
	reads('', disconnected);
	await fireEvent.click(screen.getByRole('button', { name: 'Retry account status' }));
	expect(await screen.findByRole('button', { name: 'Sign in' })).toBeTruthy();
});

it.each(['settings', 'setup'] as const)('refreshes account-wide workers and devices in the %s panel without losing offline registrations', async (variant) => {
	vi.useFakeTimers();
	let inventory = { ...profile, devices: [{ id: 'phone', name: 'Phone', platform: 'android' }] };
	api.get.mockImplementation(async (p: string) => p === '/mobile/hub/connection'
		? { ...disconnected, signed_in: true, status: 'connected' } : inventory);
	render(AccountPanel, { variant });
	await tick(); await vi.advanceTimersByTimeAsync(0);
	expect(within(screen.getByRole('list', { name: 'Phones and browsers on this account' })).getByText('Phone · android')).toBeTruthy();
	inventory = { ...profile, servers: [profile.servers[0]], devices: [{ id: 'browser', name: 'Browser', platform: 'web' }] };
	await vi.advanceTimersByTimeAsync(32_000);
	expect(screen.queryByText('Laptop')).toBeNull();
	expect(screen.queryByText('Phone · android')).toBeNull();
	expect(screen.getByText('Browser · web')).toBeTruthy();
	inventory = { ...inventory, servers: [...profile.servers, { id: 'offline', name: 'Offline PC', url: null, current: false }] };
	await fireEvent.click(screen.getByRole('button', { name: 'Refresh account devices' }));
	await tick();
	expect(screen.getByText('Offline PC')).toBeTruthy();
});

it('distinguishes an old hub from an account with no phone or browser registrations', async () => {
	reads('', { ...disconnected, signed_in: true, status: 'connected' });
	render(AccountPanel);
	expect(await screen.findByText('Update the account service to see its phones and browsers here.')).toBeTruthy();
	reads('', { ...disconnected, signed_in: true, status: 'connected' }, { ...profile, devices: [] });
	await fireEvent.click(screen.getByRole('button', { name: 'Refresh account devices' }));
	expect(await screen.findByText('No phones or browsers registered to this account.')).toBeTruthy();
});
