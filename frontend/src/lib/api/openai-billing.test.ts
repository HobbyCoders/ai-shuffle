import { describe, it, expect, vi } from 'vitest';
import { api } from './client';
import { supportsOpenAIBilling, connectOpenAIBilling, getOpenAIBillingStatus, confirmOpenAIBilling, cancelOpenAIBilling, disconnectOpenAIBilling } from './openai-billing';
import type { ModelProvider } from './model-providers';
vi.mock('./client', () => ({ api: { get: vi.fn(), post: vi.fn() } }));
const provider = { id: 'p', enabled: true, provider_type: 'openai', base_url: 'https://api.openai.com/v1' } as ModelProvider;
describe('OpenAI billing API', () => {
 it('uses only official saved enabled OpenAI providers', () => {
  expect(supportsOpenAIBilling(provider)).toBe(true);
  for (const change of [{ id: '' }, { enabled: false }, { provider_type: 'custom' }, { auth_mode: 'codex' }, { base_url: 'https://api.openai.com.evil.test/v1' }, { base_url: 'http://api.openai.com/v1' }, { base_url: 'not a URL' }]) expect(supportsOpenAIBilling({ ...provider, ...change })).toBe(false);
 });
 it('encodes provider IDs and sends the exact confirmation and cancellation bodies', async () => {
  const base = '/model-providers/p%2F1/openai-billing';
  await getOpenAIBillingStatus('p/1'); expect(api.get).toHaveBeenCalledWith(`${base}/status`);
  await connectOpenAIBilling('p/1'); expect(api.post).toHaveBeenCalledWith(`${base}/connect`);
  const body = { flow_id: 'f', observation_id: 'o', organization_id: 'org' };
  await confirmOpenAIBilling('p/1', body); expect(api.post).toHaveBeenCalledWith(`${base}/confirm`, body);
  await cancelOpenAIBilling('p/1', 'f'); expect(api.post).toHaveBeenCalledWith(`${base}/cancel`, { flow_id: 'f' });
  await disconnectOpenAIBilling('p/1'); expect(api.post).toHaveBeenCalledWith(`${base}/disconnect`);
 });
});
