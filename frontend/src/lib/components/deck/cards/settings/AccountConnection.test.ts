import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn() }));
const capability = vi.hoisted(() => ({ account_sign_in_available: true as boolean | undefined }));
vi.mock('$lib/stores/auth.svelte', () => ({ auth: { subscribe(run: (value: unknown) => void) { run({ status: capability }); return () => {}; } } }));
vi.mock('$lib/api/client', () => ({ api, getErrorMessage: (e: Error) => e.message }));
import AccountConnection from './AccountConnection.svelte';

const disconnected = { signed_in: false, status: 'disconnected', code: null, qr_svg: null, error: null };
const waiting = { ...disconnected, status: 'waiting', code: 'TESTONLYCODE', authorization_url: 'https://account.example.com/claim' };
beforeEach(() => { vi.resetAllMocks(); capability.account_sign_in_available = true; api.get.mockResolvedValue(disconnected); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

it('disables unavailable account sign-in and explains how to enable it without a login request', async () => {
    capability.account_sign_in_available = false;
    render(AccountConnection);
    const button = await screen.findByRole('button', { name: 'Log in' });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('Account sign-in is not configured on this worker. Ask the operator to enable it.')).toBeTruthy();
    expect(button.getAttribute('aria-describedby')).toBe('account-sign-in-unavailable');
    await fireEvent.click(button);
    expect(api.post).not.toHaveBeenCalled();
    expect(screen.queryByRole('link')).toBeNull();
});

it('keeps local logout available when account sign-in is unavailable', async () => {
    capability.account_sign_in_available = false;
    api.get.mockResolvedValue({ ...disconnected, signed_in: true });
    api.delete.mockResolvedValue(disconnected);
    render(AccountConnection);
    const button = await screen.findByRole('button', { name: 'Log out' });
    expect((button as HTMLButtonElement).disabled).toBe(false);
    await fireEvent.click(button);
    expect(api.delete).toHaveBeenCalledExactlyOnceWith('/mobile/hub/connection');
    expect((await screen.findByRole('button', { name: 'Log in' }) as HTMLButtonElement).disabled).toBe(true);
});

it('shows a compact logged-out row and reveals authorization only after Log in', async () => {
    render(AccountConnection, { workerName: 'Desk worker' });
    const button = await screen.findByRole('button', { name: 'Log in' });
    expect(screen.getByText('Not logged in')).toBeTruthy();
    expect(screen.queryByLabelText('Worker name')).toBeNull();
    expect(screen.queryByRole('link')).toBeNull();
    api.post.mockResolvedValue(waiting);
    await fireEvent.click(button);
    expect(api.post).toHaveBeenCalledWith('/mobile/hub/connection', { name: 'Desk worker' });
    expect((await screen.findByRole('link', { name: 'Continue to account' })).getAttribute('href')).toBe(waiting.authorization_url);
    expect(screen.getByRole('button', { name: 'Cancel log in' })).toBeTruthy();
    expect(screen.queryByText(/TEST ONLY CODE/)).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Use my phone instead' }));
    expect(screen.getByRole('button', { name: 'Copy connection code' })).toBeTruthy();
});

it.each(['connected', 'connecting', 'reconnecting', 'locked', 'unavailable'])('stays logged in and permits Log out while %s', async (status) => {
    api.get.mockResolvedValue({ ...disconnected, signed_in: true, status });
    api.delete.mockResolvedValue(disconnected);
    render(AccountConnection);
    const button = await screen.findByRole('button', { name: 'Log out' });
    expect(screen.getByText('Logged in')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Log in' })).toBeNull();
    await fireEvent.click(button);
    expect(api.delete).toHaveBeenCalledWith('/mobile/hub/connection');
    expect(await screen.findByRole('button', { name: 'Log in' })).toBeTruthy();
});

it('does not call an unknown account logged out while loading or after a read failure', async () => {
    let reject!: (reason: Error) => void;
    api.get.mockImplementation(() => new Promise((_, fail) => { reject = fail; }));
    render(AccountConnection);
    expect(screen.getByText('Checking account…')).toBeTruthy();
    expect(screen.queryByText('Not logged in')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Log in' })).toBeNull();
    reject(new Error('Account request failed'));
    await screen.findByRole('alert');
    expect(screen.getByText('Account status unavailable')).toBeTruthy();
    api.get.mockResolvedValue(disconnected);
    await fireEvent.click(screen.getByRole('button', { name: 'Retry account status' }));
    expect(await screen.findByRole('button', { name: 'Log in' })).toBeTruthy();
});

it('keeps an existing waiting grant private until Log in without creating another grant', async () => {
    api.get.mockResolvedValue(waiting);
    api.delete.mockResolvedValue(disconnected);
    render(AccountConnection);
    await screen.findByRole('button', { name: 'Log in' });
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Copy connection code' })).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Log in' }));
    expect(await screen.findByRole('link', { name: 'Continue to account' })).toBeTruthy();
    expect(api.post).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel log in' }));
    expect(api.delete).toHaveBeenCalledWith('/mobile/hub/connection');
    expect(screen.queryByRole('link')).toBeNull();
});

it('offers the phone-code fallback without an authorization URL', async () => {
    api.post.mockResolvedValue({ ...waiting, authorization_url: null });
    render(AccountConnection);
    await fireEvent.click(await screen.findByRole('button', { name: 'Log in' }));
    expect(await screen.findByRole('button', { name: 'Copy connection code' })).toBeTruthy();
    expect(screen.queryByRole('link')).toBeNull();
});

it('keeps local logout warnings visible across successful background refreshes', async () => {
    vi.useFakeTimers();
    api.get.mockResolvedValue({ ...disconnected, signed_in: true, status: 'connected' });
    api.delete.mockResolvedValue({ ...disconnected, error: 'Signed out on this worker. Remove its listing from your account.' });
    render(AccountConnection);
    await tick(); await vi.advanceTimersByTimeAsync(0);
    await fireEvent.click(screen.getByRole('button', { name: 'Log out' }));
    api.get.mockResolvedValue(disconnected);
    await vi.advanceTimersByTimeAsync(4000);
    expect(screen.getByRole('alert').textContent).toContain('Signed out on this worker.');
    expect(screen.getByRole('button', { name: 'Log in' })).toBeTruthy();
});

it('keeps account state and exposes a retry after a failed Log out', async () => {
    api.get.mockResolvedValue({ ...disconnected, signed_in: true, status: 'connected' });
    api.delete.mockRejectedValue(new Error('Log out failed'));
    render(AccountConnection);
    await fireEvent.click(await screen.findByRole('button', { name: 'Log out' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Log out failed');
    expect(screen.getByText('Logged in')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Log out' })).toBeTruthy();
});

it('ignores an old status request that resolves after Log out', async () => {
    vi.useFakeTimers();
    const connected = { ...disconnected, signed_in: true, status: 'connected' };
    api.get.mockResolvedValueOnce(connected);
    let resolve!: (value: typeof connected) => void;
    api.get.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    api.delete.mockResolvedValue(disconnected);
    render(AccountConnection);
    await tick(); await vi.advanceTimersByTimeAsync(4000);
    await fireEvent.click(screen.getByRole('button', { name: 'Log out' }));
    resolve(connected);
    await tick(); await vi.advanceTimersByTimeAsync(0);
    expect(screen.getByText('Not logged in')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Log out' })).toBeNull();
});

it('reports Log in failure and allows another attempt', async () => {
    api.post.mockRejectedValue(new Error('Account service is unavailable'));
    render(AccountConnection);
    await fireEvent.click(await screen.findByRole('button', { name: 'Log in' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Account service is unavailable');
    expect((screen.getByRole('button', { name: 'Log in' }) as HTMLButtonElement).disabled).toBe(false);
});

it('updates the compact status and hides enrollment when authorization completes', async () => {
    vi.useFakeTimers();
    api.post.mockResolvedValue(waiting);
    render(AccountConnection);
    await tick(); await vi.advanceTimersByTimeAsync(0);
    await fireEvent.click(screen.getByRole('button', { name: 'Log in' }));
    api.get.mockResolvedValue({ ...disconnected, signed_in: true, status: 'connecting' });
    await vi.advanceTimersByTimeAsync(4000);
    expect(screen.getByText('Logged in')).toBeTruthy();
    expect(screen.queryByRole('link')).toBeNull();
});
