import { beforeEach, afterEach, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/svelte';
import OpenRouterCredits from './OpenRouterCredits.svelte';
import type { ModelProvider } from '$lib/api/model-providers';
import * as billing from '$lib/api/openrouter-billing';
vi.mock('$lib/api/openrouter-billing', () => ({ getOpenRouterUsage: vi.fn(), setOpenRouterUsage: vi.fn(), disconnectOpenRouterUsage: vi.fn() }));
const provider = { id: 'p1', name: 'OpenRouter', enabled: true, provider_type: 'openrouter', base_url: 'https://openrouter.ai/api/v1' } as ModelProvider;
const initial: billing.OpenRouterUsageStatus = { configured: false, credits: null, fetched_at: null, last_error_kind: null, source: 'OpenRouter management API', scope: 'Account' };
beforeEach(() => {
 vi.clearAllMocks();
 vi.mocked(billing.getOpenRouterUsage).mockResolvedValue(initial);
 vi.mocked(billing.setOpenRouterUsage).mockResolvedValue({ ...initial, configured: true });
 vi.mocked(billing.disconnectOpenRouterUsage).mockResolvedValue(initial);
});
afterEach(cleanup);

it('explains the broader credential permissions and separates account and model key scopes', async () => {
 render(OpenRouterCredits, { provider });
 await screen.findByText('Account credit access is not configured.');
 expect(screen.getByText(/can also create, edit and delete API keys/)).toBeTruthy();
 expect(screen.getByText(/model key reports its own spend and limit separately/)).toBeTruthy();
 expect(billing.getOpenRouterUsage).toHaveBeenCalledTimes(1);
});

it.each([false, true])('clears the password after saving (failure=%s)', async (fail) => {
 if (fail) vi.mocked(billing.setOpenRouterUsage).mockRejectedValue(new Error('offline'));
 render(OpenRouterCredits, { provider });
 await screen.findByText('Account credit access is not configured.');
 const field = screen.getByLabelText('Management API key') as HTMLInputElement;
 expect(field.type).toBe('password');
 await fireEvent.input(field, { target: { value: 'test-management-key' } });
 await fireEvent.click(screen.getByRole('button', { name: 'Save management key' }));
 await waitFor(() => expect(billing.setOpenRouterUsage).toHaveBeenCalledWith('p1', 'test-management-key'));
 await waitFor(() => expect(field.value).toBe(''));
 if (fail) expect(screen.getByRole('alert').textContent).toContain('Read cached status');
});

it('retains a labeled stale account balance and removes only the management credential', async () => {
 vi.mocked(billing.getOpenRouterUsage).mockResolvedValue({ ...initial, configured: true,
  credits: { total_credits: 25, total_usage: 4.25, balance_remaining: 20.75 }, fetched_at: 1000, last_error_kind: 'billing_access_denied' });
 render(OpenRouterCredits, { provider });
 await screen.findByText('$20.75 account credit remaining');
 expect(screen.getByText(/Polling is paused/)).toBeTruthy();
 expect(screen.getByText(/Last successful update/)).toBeTruthy();
 await fireEvent.click(screen.getByRole('button', { name: 'Remove management key' }));
 await screen.findByText('Account credit access is not configured.');
 expect(billing.disconnectOpenRouterUsage).toHaveBeenCalledWith('p1');
});
