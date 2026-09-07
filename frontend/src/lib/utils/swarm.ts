/** Validated Swarm snapshots shared by tool results, live events and hydration. */
export type SwarmStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted';
export interface SwarmTask {
 id: string; title: string; agent: string; depends_on: string[];
 status: SwarmStatus | 'blocked'; task_id?: string | null; worker_session_id?: string | null;
 result?: string | null; error?: string | null; started_at?: string | null; finished_at?: string | null;
}
export interface SwarmSnapshot {
 id: string; session_id: string; title: string; status: SwarmStatus;
 created_at: string; updated_at: string; tasks: SwarmTask[];
}
const statuses = new Set(['queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted']);
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
export function isSwarmTool(name: string): boolean {
 return name.split('__').at(-1)?.toLowerCase() === 'swarm';
}
export function parseSwarm(value: unknown): SwarmSnapshot | null {
 if (typeof value === 'string') { try { value = JSON.parse(value); } catch { return null; } }
 if (record(value) && 'swarm' in value) value = value.swarm;
 if (!record(value) || !['id', 'session_id', 'title', 'created_at', 'updated_at'].every(k => typeof value[k] === 'string') ||
  !value.id || !value.session_id || !statuses.has(String(value.status)) || !Array.isArray(value.tasks)) return null;
 if (!Number.isFinite(Date.parse(String(value.updated_at))) || !Number.isFinite(Date.parse(String(value.created_at)))) return null;
 const ids = new Set<string>();
 for (const t of value.tasks) {
  if (!record(t) || typeof t.id !== 'string' || !t.id || ids.has(t.id) || typeof t.title !== 'string' ||
   typeof t.agent !== 'string' || !Array.isArray(t.depends_on) || !t.depends_on.every(d => typeof d === 'string') ||
   !(statuses.has(String(t.status)) || t.status === 'blocked')) return null;
  for (const key of ['task_id', 'worker_session_id', 'result', 'error', 'started_at', 'finished_at']) {
   if (t[key] != null && typeof t[key] !== 'string') return null;
  }
  ids.add(t.id);
 }
 return value as unknown as SwarmSnapshot;
}
export const isSwarmActive = (s: SwarmSnapshot) => s.status === 'queued' || s.status === 'running';
/** Older history and slow HTTP responses must never roll live work backwards. */
export function newestSwarm(current: SwarmSnapshot | undefined, next: SwarmSnapshot): SwarmSnapshot {
 if (!current) return next;
 const delta = Date.parse(next.updated_at) - Date.parse(current.updated_at);
 if (delta < 0 || (delta === 0 && !isSwarmActive(current) && isSwarmActive(next))) return current;
 return next;
}
/** Kahn layers: each group may run in parallel; cycles/missing dependencies stay explicit. */
export function swarmStages(tasks: SwarmTask[]): { tasks: SwarmTask[]; unresolved: boolean }[] {
 const done = new Set<string>();
 let remaining = [...tasks];
 const stages: { tasks: SwarmTask[]; unresolved: boolean }[] = [];
 while (remaining.length) {
  const ready = remaining.filter(t => t.depends_on.every(id => done.has(id)));
  if (!ready.length) { stages.push({ tasks: remaining, unresolved: true }); break; }
  stages.push({ tasks: ready, unresolved: false });
  ready.forEach(t => done.add(t.id));
  remaining = remaining.filter(t => !done.has(t.id));
 }
 return stages;
}
export function resolveSwarm(swarms: SwarmSnapshot[], result: SwarmSnapshot | null, input?: Record<string, unknown>): SwarmSnapshot | null {
 const id = result?.id ?? (typeof input?.swarm_id === 'string' ? input.swarm_id : null);
 if (id) { const live = swarms.find(s => s.id === id); return live && result ? newestSwarm(result, live) : live ?? result; }
 // Before a result identifies the workflow, only bind an unambiguous exact plan.
 if (!Array.isArray(input?.tasks) || !input.tasks.length) return null;
 const ids = input.tasks.map(t => record(t) ? t.id : undefined);
 const matches = swarms.filter(s => (!input.title || input.title === s.title) && s.tasks.length === ids.length && s.tasks.every(t => ids.includes(t.id)));
 return matches.length === 1 ? matches[0] : null;
}
