import { api } from './client';
import type { ModelProvider } from './model-providers';

export interface ManualBalance {
 balance_remaining: number;
 manual_balance: number;
 estimated_spend: number;
 set_at: number;
 unpriced_turns: number;
}
export interface OpenAIUsageStatus {
 manual_balance?: ManualBalance | null;
 configured: boolean;
 organization_id: string | null;
 source: string;
 scope: string;
 spend: { spend_month: number; input_tokens: number; output_tokens: number; num_model_requests: number } | null;
 fetched_at: number | null;
 last_error_kind: string | null;
}
export function supportsOpenAIBilling(provider: ModelProvider): boolean {
 if (provider.provider_type !== 'openai' || !provider.enabled || !provider.id || ['codex', 'chatgpt'].includes(provider.auth_mode ?? '')) return false;
 try {
  const url = new URL(provider.base_url);
  return url.protocol === 'https:' && url.hostname === 'api.openai.com' && !url.port && !url.username && !url.password;
 } catch { return false; }
}
const path = (id: string) => `/model-providers/${encodeURIComponent(id)}/openai-usage`;
export const setOpenAIBalance = (id: string, balance_usd: number): Promise<OpenAIUsageStatus> => api.put(`${path(id)}/balance`, { balance_usd });
export const clearOpenAIBalance = async (id: string): Promise<OpenAIUsageStatus> => {
 await api.delete(`${path(id)}/balance`);
 return getOpenAIUsage(id);
};
export const getOpenAIUsage = (id: string): Promise<OpenAIUsageStatus> => api.get(path(id));
export const setOpenAIUsage = (id: string, admin_key: string, organization_id: string): Promise<OpenAIUsageStatus> => api.put(path(id), { admin_key, organization_id });
export const disconnectOpenAIUsage = async (id: string): Promise<OpenAIUsageStatus> => {
 await api.delete(path(id));
 return getOpenAIUsage(id);
};
