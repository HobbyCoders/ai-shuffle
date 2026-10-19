import { api } from './client';

export interface ProviderBalance {
 configured: boolean;
 source: string;
 source_id: string;
 fetched_at: number | null;
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
export const startPlatformSignIn = (id: string): Promise<{ ticket: string }> => api.post(`${balancePath(id)}/sign-in`, {});
