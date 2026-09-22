import { api } from './client';
import type { ModelProvider } from './model-providers';

export interface OpenRouterUsageStatus {
 configured: boolean;
 source: string;
 scope: string;
 credits: { total_credits: number; total_usage: number; balance_remaining: number } | null;
 fetched_at: number | null;
 last_error_kind: string | null;
}
export function supportsOpenRouterBilling(provider: ModelProvider): boolean {
 if (provider.provider_type !== 'openrouter' || !provider.enabled || !provider.id) return false;
 try {
  const url = new URL(provider.base_url);
  return url.protocol === 'https:' && url.hostname === 'openrouter.ai' && !url.port && !url.username && !url.password;
 } catch { return false; }
}
const path = (id: string) => `/model-providers/${encodeURIComponent(id)}/openrouter-usage`;
export const getOpenRouterUsage = (id: string): Promise<OpenRouterUsageStatus> => api.get(path(id));
export const setOpenRouterUsage = (id: string, management_key: string): Promise<OpenRouterUsageStatus> => api.put(path(id), { management_key });
export const disconnectOpenRouterUsage = async (id: string): Promise<OpenRouterUsageStatus> => {
 await api.delete(path(id));
 return getOpenRouterUsage(id);
};
