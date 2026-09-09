import { writable, get } from 'svelte/store';
import { apiUrl, withAuth } from '$lib/api/runtime';
import { parseSwarm, newestSwarm, type SwarmSnapshot } from '$lib/utils/swarm';
import { recheckAuthOn401 } from '$lib/api/client';

interface SwarmState { bySession: Record<string, Record<string, SwarmSnapshot>>; errors: Record<string, string>; }
const state = writable<SwarmState>({ bySession: {}, errors: {} });
const pending = new Map<string, Promise<void>>();
// Swarm endpoints intentionally use /api/sessions (not the legacy /api/v1 client).
const base = (id: string) => apiUrl(`/api/sessions/${encodeURIComponent(id)}/swarms`);
async function request(url: string, method = 'GET'): Promise<unknown> {
 const response = await fetch(url, withAuth({ method, headers: { Accept: 'application/json' } }));
 if (response.status === 401) recheckAuthOn401();
 if (!response.ok) throw new Error(`Swarm request failed (${response.status}). Try again.`);
 return response.json();
}
function receive(value: unknown, sessionId?: string) {
 const swarm = parseSwarm(value);
 if (!swarm || (sessionId && swarm.session_id !== sessionId)) return;
 state.update(s => {
  const previous = s.bySession[swarm.session_id] ?? {};
  const next = newestSwarm(previous[swarm.id], swarm);
  if (next === previous[swarm.id]) return s;
  return { ...s, bySession: { ...s.bySession, [swarm.session_id]: { ...previous, [swarm.id]: next } } };
 });
}
function hydrate(sessionId: string): Promise<void> {
 const existing = pending.get(sessionId);
 if (existing) return existing;
 const work = (async () => {
  try {
   const data = await request(base(sessionId));
   if (!data || typeof data !== 'object' || !('swarms' in data) || !Array.isArray(data.swarms)) throw new Error('Unable to read workflow updates. Try again.');
   for (const item of data.swarms) {
    const snapshot = parseSwarm(item);
    if (!snapshot || snapshot.session_id !== sessionId) throw new Error('Unable to read workflow updates. Try again.');
    receive(snapshot, sessionId);
   }
   state.update(s => ({ ...s, errors: { ...s.errors, [sessionId]: '' } }));
  } catch (error) {
   state.update(s => ({ ...s, errors: { ...s.errors, [sessionId]: error instanceof Error ? error.message : 'Unable to refresh workflow. Try again.' } }));
  } finally { pending.delete(sessionId); }
 })();
 pending.set(sessionId, work);
 return work;
}
export const swarms = {
 subscribe: state.subscribe, receive, hydrate,
 async cancel(sessionId: string, swarmId: string) {
  const result = parseSwarm(await request(`${base(sessionId)}/${encodeURIComponent(swarmId)}/cancel`, 'POST'));
  if (!result || result.id !== swarmId || result.session_id !== sessionId) throw new Error('Cancellation was not confirmed. Refresh the workflow and try again.');
  receive(result, sessionId);
 },
 /** Reconnect refresh is scoped to sessions with workflows, not every chat. */
 refreshKnown(sessionId: string) {
  if (Object.keys(get(state).bySession[sessionId] ?? {}).length) void hydrate(sessionId);
 }
};
