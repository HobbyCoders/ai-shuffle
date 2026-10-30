import { api } from './client';
import type { ModelProvider } from './model-providers';

export function supportsXAIBalance(provider: ModelProvider): boolean {
 if (provider.provider_type !== 'xai' || !provider.enabled || !provider.id || provider.auth_mode === 'grok') return false;
 try {
  const url = new URL(provider.base_url);
  return url.protocol === 'https:' && url.hostname === 'api.x.ai' && !url.port && !url.username && !url.password;
 } catch { return false; }
}

export interface ProviderBalance {
 configured: boolean;
 source: string;
 source_id: string;
 fetched_at: number | null;
 fresh_until: number | null;
 refreshing?: boolean;
 last_error_scope?: string | null;
 last_error_kind: string | null;
 needs_reauth: boolean;
 status: string;
 credits: { balance_remaining: number; spend_month?: number; spending_limit?: number } | null;
}
export const balancePath = (id: string) => `/model-providers/${encodeURIComponent(id)}/balance-access`;
export const getProviderBalance = (id: string): Promise<ProviderBalance> => api.get(balancePath(id));
export const saveProviderBalance = (id: string, key: string): Promise<ProviderBalance> => api.put(balancePath(id), { key });
export const removeProviderBalance = async (id: string): Promise<ProviderBalance> => {
 await api.delete(balancePath(id));
 return getProviderBalance(id);
};
