/** Coalesced invalidations. A subscription exists only while its view is mounted.
 * Callbacks refresh lists, never replace editor buffers. Bursts during a request
 * schedule one subsequent refresh so the last mutation cannot be missed.
 */
export const RESOURCE_NAMES = ['sessions', 'projects', 'agents', 'tags', 'notes', 'templates', 'settings', 'connectors', 'automations', 'plugins', 'hooks', 'mcp', 'notifications', 'api_users'] as const;
export type ResourceName = typeof RESOURCE_NAMES[number];
type Subscriber = { names: readonly string[]; refresh: () => void | Promise<unknown>; dirty: boolean; running: boolean; alive: boolean };
export class ResourceSync {
 private subscribers = new Set<Subscriber>();
 private timer: ReturnType<typeof setTimeout> | undefined;
 subscribe(names: readonly string[], refresh: () => void | Promise<unknown>) {
  const subscriber: Subscriber = { names, refresh, dirty: false, running: false, alive: true };
  this.subscribers.add(subscriber);
  return () => { subscriber.alive = false; this.subscribers.delete(subscriber); };
 }
 invalidate(names: readonly string[] = RESOURCE_NAMES) {
  for (const s of this.subscribers) if (s.names.some(n => names.includes(n))) s.dirty = true;
  if (!this.timer) this.timer = setTimeout(() => {
   this.timer = undefined;
   for (const s of this.subscribers) void this.run(s);
  }, 100);
 }
 clear() {
  if (this.timer) clearTimeout(this.timer);
  this.timer = undefined;
  for (const s of this.subscribers) s.dirty = false;
 }
 private async run(s: Subscriber) {
  if (!s.alive || s.running || !s.dirty) return;
  s.running = true; s.dirty = false;
  try { await s.refresh(); } catch { /* next invalidation/reconnect retries */ }
  finally { s.running = false; if (s.dirty && s.alive) void this.run(s); }
 }
}
export const resourceSync = new ResourceSync();
