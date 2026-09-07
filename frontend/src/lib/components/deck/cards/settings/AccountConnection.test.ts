import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api, getErrorMessage: (e: Error) => e.message }));
import AccountConnection from './AccountConnection.svelte';

const disconnected = { signed_in: false, status: 'disconnected', code: null, error: null };
beforeEach(() => { vi.resetAllMocks(); api.get.mockResolvedValue(disconnected); });
afterEach(() => cleanup());

it('offers account sign-in for this worker', async () => {
    render(AccountConnection);
    const button = await screen.findByRole('button', { name: 'Sign in to Shuffle' });
    api.post.mockResolvedValue({ ...disconnected, status: 'waiting', code: 'ABCDEFGHIJKL', authorization_url: 'https://account.example.com/claim' });
    await fireEvent.click(button);
    expect(api.post).toHaveBeenCalledWith('/mobile/hub/connection', { name: 'My worker' });
    expect(await screen.findByRole('link', { name: 'Sign in and link this worker' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Cancel sign-in' })).toBeTruthy();
});

it.each(['connected', 'reconnecting', 'locked', 'unavailable'])('allows sign-out while %s', async (status) => {
    api.get.mockResolvedValue({ ...disconnected, signed_in: true, status });
    api.delete.mockResolvedValue(disconnected);
    render(AccountConnection);
    await fireEvent.click(await screen.findByRole('button', { name: 'Sign out of this worker' }));
    expect(api.delete).toHaveBeenCalledWith('/mobile/hub/connection');
    expect(await screen.findByRole('button', { name: 'Sign in to Shuffle' })).toBeTruthy();
});

it('reports local sign-out when the account service is offline', async () => {
    api.get.mockResolvedValue({ ...disconnected, signed_in: true, status: 'connected' });
    api.delete.mockResolvedValue({ ...disconnected, error: 'Signed out on this worker. Remove its listing from your account.' });
    render(AccountConnection);
    await fireEvent.click(await screen.findByRole('button', { name: 'Sign out of this worker' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Signed out on this worker.');
    expect(screen.getByRole('button', { name: 'Sign in to Shuffle' })).toBeTruthy();
});
