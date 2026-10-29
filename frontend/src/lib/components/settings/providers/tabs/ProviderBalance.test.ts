import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import ProviderBalance from './ProviderBalance.svelte';

const mocks = vi.hoisted(() => ({ get: vi.fn(), save: vi.fn(), remove: vi.fn(),  }));
vi.mock('$lib/api/provider-balances', () => ({
 balancePath: (id: string) => `/model-providers/${encodeURIComponent(id)}/balance-access`,
 getProviderBalance: mocks.get, saveProviderBalance: mocks.save,
 removeProviderBalance: mocks.remove
}));
const status = { configured: true, source: 'xAI Management API', fetched_at: Date.now(), fresh_until: Date.now() + 600_000,
 status: 'fresh', needs_reauth: false, credits: { balance_remaining: 12.34 } };
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.unstubAllGlobals(); });

it('shows xAI credit and fetched time; writes but never reads the management key', async () => {
 mocks.get.mockResolvedValue(status); mocks.save.mockResolvedValue(status);
 render(ProviderBalance, { providerId: 'p1' });
 await screen.findByText('$12.34');
 expect(screen.getByText(/Last fetched:/)).toBeTruthy();
 expect(screen.getByText('xAI Management API')).toBeTruthy();
 expect(document.body.textContent).not.toMatch(/unofficial/i);
 const input = screen.getByLabelText('xAI management key') as HTMLInputElement;
 expect(input.type).toBe('password');
 await fireEvent.input(input, { target: { value: 'management-test-only' } });
 await fireEvent.click(screen.getByRole('button', { name: 'Save access' }));
 await waitFor(() => expect(mocks.save).toHaveBeenCalledWith('p1', 'management-test-only'));
 expect(input.value).toBe('');
});
it('refused xAI management access hides the stale amount and asks for a replacement key', async () => {
 mocks.get.mockResolvedValue({ ...status, credits: null, needs_reauth: true, status: 'unavailable' });
 render(ProviderBalance, { providerId: 'p1' });
 await screen.findByText(/Management access refused/);
 expect(screen.queryByText('$12.34')).toBeNull();
 expect(screen.getByLabelText('xAI management key')).toBeTruthy();
});
it('xAI provides management-key controls, cycle spend and limit, not OpenAI sign-in', async () => {
 mocks.get.mockResolvedValue({ ...status, source: 'xAI Management API', credits: { balance_remaining: 0, spend_month: 3, spending_limit: 200 } });
 mocks.remove.mockResolvedValue({ ...status, configured: false, credits: null });
 render(ProviderBalance, { providerId: 'x1' });
 await screen.findByText('$0.00');
 expect(screen.getByText('$3.00')).toBeTruthy();
 expect(screen.getByText('$200.00')).toBeTruthy();
 expect(screen.getByLabelText('xAI management key')).toBeTruthy();
 expect(screen.queryByRole('button', { name: 'Sign in to OpenAI Platform' })).toBeNull();
 await fireEvent.click(screen.getByRole('button', { name: 'Disconnect balance' }));
 await waitFor(() => expect(mocks.remove).toHaveBeenCalledWith('x1'));
});
it.each([
 { last_error_kind: 'network_error' },
 { needs_reauth: true },
 { fresh_until: Date.now() - 1 },
 { status: 'stale' },
])('suppresses non-current credit even when the response retains an amount: %j', async (patch) => {
 mocks.get.mockResolvedValue({ ...status, ...patch });
 render(ProviderBalance, { providerId: 'p1' });
 await screen.findByText(/Last fetched:/);
 expect(screen.queryByText('$12.34')).toBeNull();
});
it('suppresses the previous amount when a status refresh fails', async () => {
 mocks.get.mockResolvedValueOnce(status).mockRejectedValueOnce(new Error('Status unavailable')).mockResolvedValueOnce(status);
 render(ProviderBalance, { providerId: 'p1' });
 await screen.findByText('$12.34');
 await fireEvent.click(screen.getByRole('button', { name: 'Refresh balance status' }));
 await screen.findByRole('alert');
 expect(screen.queryByText('$12.34')).toBeNull();
 await fireEvent.click(screen.getByRole('button', { name: 'Refresh balance status' }));
 await screen.findByText('$12.34');
 expect(screen.queryByRole('alert')).toBeNull();
});
it('re-reads while a requested balance poll runs and keeps readings through jitter', async () => {
 mocks.get.mockResolvedValueOnce({ ...status, refreshing: true, fetched_at: Date.now() - 330_000 })
  .mockResolvedValueOnce({ ...status, refreshing: false, credits: { balance_remaining: 23.45 } });
 render(ProviderBalance, { providerId: 'p1' });
 await screen.findByText('$12.34');
 await waitFor(() => expect(screen.getByText('$23.45')).toBeTruthy(), { timeout: 2500 });
 expect(mocks.get).toHaveBeenCalledTimes(2);
});
