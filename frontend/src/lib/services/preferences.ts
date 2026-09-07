import { api } from '$lib/api/client';
import { PreferenceSync } from './preference-sync';
export const preferenceSync = new PreferenceSync({
 all: async () => (await api.get<{ preferences: Record<string, unknown> }>('/preferences/all')).preferences,
 allWithVersions: () => api.get<{ preferences: Record<string, unknown>; versions: Record<string, string | null> }>('/preferences/all'),
 get: async key => {
  try { return await api.get<{ value: unknown; version: string | null }>(`/preferences/${encodeURIComponent(key)}`); }
  catch (error) { if ((error as { status?: number }).status === 404) return null; throw error; }
 },
 put: (key, value, onlyIfMissing, expectedVersion) => api.put<{ value: unknown; version: string | null }>(
  `/preferences/${encodeURIComponent(key)}${onlyIfMissing ? '?only_if_missing=true' : ''}`,
  { key, value, ...(expectedVersion !== undefined ? {expected_version: expectedVersion} : {}) })
});
