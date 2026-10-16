/** Coalesced invalidations. A subscription exists only while its view is mounted.
 * Callbacks refresh lists, never replace editor buffers. Bursts during a request
 * schedule one subsequent refresh so the last mutation cannot be missed.
 *
 * While the document is hidden, invalidations only mark subscribers dirty: the
 * server's periodic reconcile and other tabs' edits must not refetch lists
 * nobody can see (#1282). The first return to visibility refreshes each dirty
 * subscriber once. A subscriber that only drops a local cache (no request) can
 * opt into hidden delivery with `{ whileHidden: true }`.
 */
export const RESOURCE_NAMES = ['sessions', 'projects', 'agents', 'tags', 'notes', 'templates', 'settings', 'connectors', 'automations', 'plugins', 'hooks', 'mcp', 'notifications', 'api_users', 'network', 'chat-access', 'assistant'] as const;
export type ResourceName = typeof RESOURCE_NAMES[number];
type Subscriber = { names: readonly string[]; refresh: () => void | Promise<unknown>; dirty: boolean; running: boolean; alive: boolean; whileHidden: boolean };
const hidden = () => typeof document !== 'undefined' && document.hidden;
export class ResourceSync {
 private subscribers = new Set<Subscriber>();
 private timer: ReturnType<typeof setTimeout> | undefined;
 private resumeArmed = false;
 subscribe(names: readonly string[], refresh: () => void | Promise<unknown>, options: { whileHidden?: boolean } = {}) {
  const subscriber: Subscriber = { names, refresh, dirty: false, running: false, alive: true, whileHidden: !!options.whileHidden };
  this.subscribers.add(subscriber);
  return () => { subscriber.alive = false; this.subscribers.delete(subscriber); };
 }
 invalidate(names: readonly string[] = RESOURCE_NAMES) {
  for (const s of this.subscribers) if (s.names.some(n => names.includes(n))) s.dirty = true;
  this.schedule();
 }
 clear() {
  if (this.timer) clearTimeout(this.timer);
  this.timer = undefined;
  for (const s of this.subscribers) s.dirty = false;
 }
 private schedule() {
  if (!this.timer) this.timer = setTimeout(() => {
   this.timer = undefined;
   for (const s of this.subscribers) void this.run(s);
  }, 100);
 }
 private readonly onVisible = () => {
  if (hidden()) return;
  this.resumeArmed = false;
  document.removeEventListener('visibilitychange', this.onVisible);
  if ([...this.subscribers].some(s => s.dirty)) this.schedule();
 };
 private deferUntilVisible() {
  if (this.resumeArmed) return;
  this.resumeArmed = true;
  document.addEventListener('visibilitychange', this.onVisible);
 }
 private async run(s: Subscriber) {
  if (!s.alive || s.running || !s.dirty) return;
  // Stays dirty: the visibility return refreshes it once.
  if (!s.whileHidden && hidden()) { this.deferUntilVisible(); return; }
  s.running = true; s.dirty = false;
  try { await s.refresh(); } catch { /* next invalidation/reconnect retries */ }
  finally { s.running = false; if (s.dirty && s.alive) void this.run(s); }
 }
}
export const resourceSync = new ResourceSync();
