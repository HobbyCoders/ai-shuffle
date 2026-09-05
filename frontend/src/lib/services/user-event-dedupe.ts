// Only cross-transport duplicates are suppressed. Identical successive events
// on one transport remain legitimate (e.g. two clears of the same session).
const seen = new Map<string, { source: string; time: number }>();
export function acceptUserEvent(data: Record<string, unknown>, source: 'chat' | 'global'): boolean {
 if (!['user_session_created', 'user_session_updated', 'user_session_deleted', 'user_session_cleared', 'notification'].includes(String(data.type))) return true;
 const now = Date.now();
 for (const [key, entry] of seen) if (now - entry.time > 5000) seen.delete(key);
 const key = JSON.stringify(data);
 const previous = seen.get(key);
 if (previous && previous.source !== source) { seen.delete(key); return false; }
 seen.set(key, { source, time: now });
 return true;
}
export function resetUserEventDedupe() { seen.clear(); }
