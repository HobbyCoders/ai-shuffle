/** A lost HTTP reply may be retried briefly, never an hours-later owner send. */
interface PendingRetry { fingerprint: string; id: string; createdAt: number }
const TTL_MS = 5 * 60 * 1000;
const fallback = new Map<string, PendingRetry>();
const key = (sid: string) => `compact-first-retry:${sid}`;
function read(sid: string): PendingRetry | null {
	try { return JSON.parse(sessionStorage.getItem(key(sid)) || 'null'); }
	catch { return fallback.get(sid) ?? null; }
}
export function compactRetryId(sid: string, fingerprint: string, newId: () => string): string {
	const previous = read(sid);
	const now = Date.now();
	const reusable = previous?.fingerprint === fingerprint && /^client-[0-9a-f]{32}$/.test(previous.id)
		&& Number.isFinite(previous.createdAt) && now >= previous.createdAt && now - previous.createdAt < TTL_MS;
	const pending = reusable ? previous! : { fingerprint, id: newId(), createdAt: now };
	fallback.set(sid, pending);
	try { sessionStorage.setItem(key(sid), JSON.stringify(pending)); } catch { /* memory fallback */ }
	return pending.id;
}
export function clearCompactRetry(sid: string, observedId?: string) {
	if (observedId !== undefined && read(sid)?.id !== observedId) return;
	fallback.delete(sid);
	try { sessionStorage.removeItem(key(sid)); } catch { /* storage unavailable */ }
}
