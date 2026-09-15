import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/svelte';
import OpenAICredits from './OpenAICredits.svelte';
import type { ModelProvider } from '$lib/api/model-providers';
import * as billing from '$lib/api/openai-billing';
vi.mock('$lib/api/openai-billing', () => ({ getOpenAIUsage: vi.fn(), setOpenAIUsage: vi.fn(), disconnectOpenAIUsage: vi.fn() }));
const provider = { id: 'p1', name: 'OpenAI', enabled: true, provider_type: 'openai', base_url: 'https://api.openai.com/v1' } as ModelProvider;
const initial: billing.OpenAIUsageStatus = { configured: false, organization_id: null, spend: null, fetched_at: null, last_error_kind: null, source: 'OpenAI organization Admin API', scope: 'Organization' };
beforeEach(() => {
 vi.clearAllMocks();
 vi.mocked(billing.getOpenAIUsage).mockResolvedValue(initial);
 vi.mocked(billing.setOpenAIUsage).mockResolvedValue({ ...initial, configured: true, organization_id: 'org-example' });
 vi.mocked(billing.disconnectOpenAIUsage).mockResolvedValue(initial);
});
afterEach(() => { cleanup(); vi.useRealTimers(); });
describe('OpenAI Admin usage configuration', () => {
 it('has no sign-in browser, automatic timer or prepaid balance promise', async () => {
  render(OpenAICredits, { provider });
  await screen.findByText(/Admin usage access is not configured/);
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.queryByRole('button', { name: /Sign in/ })).toBeNull();
  expect(screen.getByText(/does not retrieve your prepaid credit balance/)).toBeTruthy();
  expect(billing.getOpenAIUsage).toHaveBeenCalledTimes(1);
 });
 it('sends a separate admin key and clears the password field after saving', async () => {
  render(OpenAICredits, { provider });
  await screen.findByText(/Admin usage access is not configured/);
  await fireEvent.input(screen.getByLabelText('Organization ID'), { target: { value: 'org-example' } });
  await fireEvent.input(screen.getByLabelText('Admin API key'), { target: { value: 'test-admin-key' } });
  await fireEvent.click(screen.getByRole('button', { name: 'Save admin key' }));
  await waitFor(() => expect(billing.setOpenAIUsage).toHaveBeenCalledWith('p1', 'test-admin-key', 'org-example'));
  await waitFor(() => expect((screen.getByLabelText('Admin API key') as HTMLInputElement).value).toBe(''));
 });
 it('labels spend, scope and denied access and requires confirmation to disconnect', async () => {
  vi.mocked(billing.getOpenAIUsage).mockResolvedValue({ ...initial, configured: true, organization_id: 'org-example', last_error_kind: 'billing_access_denied', fetched_at: 1000, spend: { spend_month: 12, input_tokens: 34, output_tokens: 56, num_model_requests: 7 } });
  render(OpenAICredits, { provider });
  await screen.findByText(/12.00 organization spend/);
  expect(screen.getByText(/Polling is paused/)).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: 'Disconnect usage' }));
  expect(billing.disconnectOpenAIUsage).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(billing.disconnectOpenAIUsage).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole('button', { name: 'Disconnect usage' }));
  await fireEvent.click(screen.getByRole('button', { name: 'Remove admin key' }));
  await waitFor(() => expect(billing.disconnectOpenAIUsage).toHaveBeenCalledWith('p1'));
 });
});
