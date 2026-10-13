/** Merge defaults without discarding fields introduced by newer clients. */
export function compatible<T extends object>(defaults: T, raw: unknown): T {
 const value = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw as Record<string, unknown> : {};
 const result = { ...value } as Record<string, unknown>;
 for (const [key, fallback] of Object.entries(defaults)) {
  const next = value[key];
  result[key] = fallback && typeof fallback === 'object' && !Array.isArray(fallback)
   ? compatible(fallback, next)
   : typeof next === typeof fallback && (!Array.isArray(fallback) || Array.isArray(next)) ? next : fallback;
 }
 return result as T;
}
