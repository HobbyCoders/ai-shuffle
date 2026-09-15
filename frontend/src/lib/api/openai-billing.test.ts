import { describe, it, expect, vi } from 'vitest';
import { api } from './client';
import { supportsOpenAIBilling, getOpenAIUsage, setOpenAIUsage, disconnectOpenAIUsage } from './openai-billing';
import type { ModelProvider } from './model-providers';
vi.mock('./client', () => ({ api: { get: vi.fn(), put: vi.fn(), delete: vi.fn() } }));
const provider = { id: 'p', enabled: true, provider_type: 'openai', base_url: 'https://api.openai.com/v1' } as ModelProvider;
describe('OpenAI Admin usage API', () => {
 it('uses only official saved enabled OpenAI providers', () => {
  expect(supportsOpenAIBilling(provider)).toBe(true);
  for (const change of [{ id: '' }, { enabled: false }, { provider_type: 'custom' }, { auth_mode: 'codex' }, { auth_mode: 'chatgpt' }, { base_url: 'https://api.openai.com.evil.test/v1' }, { base_url: 'http://api.openai.com/v1' }, { base_url: 'not a URL' }]) expect(supportsOpenAIBilling({ ...provider, ...change })).toBe(false);
 });
 it('encodes IDs and sends the admin secret only in the configuration body', async () => {
  const path = '/model-providers/p%2F1/openai-usage';
  await getOpenAIUsage('p/1'); expect(api.get).toHaveBeenCalledWith(path);
  await setOpenAIUsage('p/1', 'secret', 'org-test'); expect(api.put).toHaveBeenCalledWith(path, { admin_key: 'secret', organization_id: 'org-test' });
  await disconnectOpenAIUsage('p/1'); expect(api.delete).toHaveBeenCalledWith(path);
 });
});
