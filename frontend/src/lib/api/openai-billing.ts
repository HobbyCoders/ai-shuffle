import { api } from './client';
import type { ModelProvider } from './model-providers';

export interface OpenAIBillingStatus {
 status: 'not_connected' | 'pending' | 'ready' | 'connected' | 'reconnect_required' | 'unavailable' | 'other_installation';
 flow_id: string | null;
 observation_id: string | null;
 organization_id: string | null;
 credits: { balance_remaining: number; [key: string]: unknown } | null;
 fetched_at: number | null;
 error_kind: string | null;
 owner_instance_id: string | null;
 browser_available: boolean;
}
export function supportsOpenAIBilling(provider: ModelProvider): boolean {
 if (provider.provider_type !== 'openai' || !provider.enabled || !provider.id || provider.auth_mode === 'codex') return false;
 try {
  const url = new URL(provider.base_url);
  return url.protocol === 'https:' && url.hostname === 'api.openai.com' && !url.port && !url.username && !url.password;
 } catch { return false; }
}
const path = (id: string) => `/model-providers/${encodeURIComponent(id)}/openai-billing`;
export const getOpenAIBillingStatus = (id: string): Promise<OpenAIBillingStatus> => api.get(`${path(id)}/status`);
export const connectOpenAIBilling = (id: string): Promise<OpenAIBillingStatus> => api.post(`${path(id)}/connect`);
export const confirmOpenAIBilling = (id: string, body: { flow_id: string; observation_id: string; organization_id: string }): Promise<OpenAIBillingStatus> => api.post(`${path(id)}/confirm`, body);
export const cancelOpenAIBilling = (id: string, flow_id: string): Promise<OpenAIBillingStatus> => api.post(`${path(id)}/cancel`, { flow_id });
export const disconnectOpenAIBilling = (id: string): Promise<OpenAIBillingStatus> => api.post(`${path(id)}/disconnect`);
