import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/svelte';
const calls = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: calls }));
import PlaidConnection from './PlaidConnection.svelte';
import GoogleConnection from './GoogleConnection.svelte';
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
beforeEach(() => { calls.get.mockReset(); calls.put.mockReset(); calls.post.mockReset(); });
it('explains setup, read-only access, and sandbox versus real data', async () => {
    calls.get.mockResolvedValue({ configured: true, environment: 'sandbox', suggested_redirect_uri: 'https://app.example/plaid-link' });
    const page = render(PlaidConnection, { onConnected: vi.fn() });
    await page.findByRole('button', { name: 'Connect bank with Plaid' });
    expect(page.getByText(/sample transactions only/)).toBeTruthy();
    expect(page.getByText(/No payments, transfers/)).toBeTruthy();
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    await fireEvent.click(page.getByRole('button', { name: 'Connect bank with Plaid' }));
    expect(open).toHaveBeenCalledWith('/plaid-link', '_blank', expect.any(String));
    expect(page.getByRole('alert').textContent).toContain('Allow pop-ups');
});
it('requires app setup and keeps the secret masked', async () => {
    calls.get.mockResolvedValue({ configured: false, suggested_redirect_uri: 'https://app.example/plaid-link' });
    const page = render(PlaidConnection, { onConnected: vi.fn() });
    await page.findByText(/One-time administrator setup/);
    expect(page.queryByRole('button', { name: 'Connect bank with Plaid' })).toBeNull();
    await fireEvent.click(page.getByRole('button', { name: 'Plaid app setup' }));
    expect(page.getByLabelText('Secret').getAttribute('type')).toBe('password');
    expect((page.getByLabelText('OAuth return URI') as HTMLInputElement).value).toBe('https://app.example/plaid-link');
    expect((page.getByRole('button', { name: 'Save Plaid app' }) as HTMLButtonElement).disabled).toBe(true);
});
it('does not silently replace a connected institution', async () => {
    calls.get.mockResolvedValue({ configured: true, environment: 'production', suggested_redirect_uri: '' });
    const page = render(PlaidConnection, { connected: true, onConnected: vi.fn() });
    await page.findByText(/This connection is saved/);
    expect(page.queryByRole('button', { name: 'Connect bank with Plaid' })).toBeNull();
});
it('Gmail explains account access instead of bot setup', async () => {
    calls.get.mockResolvedValue({ configured: true, client_id: 'id', redirect_uri: '', suggested_redirect_uri: '', environment_managed: true });
    const page = render(GoogleConnection, { onConnected: vi.fn() });
    await page.findByRole('button', { name: 'Connect with Google' });
    expect(page.getByText(/not a bot/)).toBeTruthy();
});
