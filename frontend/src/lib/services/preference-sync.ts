/** Authoritative snapshots, serialized per-key edits, and atomic legacy migration.
 * No store setter is called during remote application; adapters apply without echo.
 */
export interface PreferenceAdapter {
 read(): unknown;
 apply(value: unknown): void; // undefined means reset to defaults
}
export interface PreferenceTransport {
 all(): Promise<Record<string, unknown>>;
 allWithVersions?(): Promise<{ preferences: Record<string, unknown>; versions: Record<string, string | null> }>;
 get(key: string): Promise<{ value: unknown; version?: string | null } | null>;
 put(key: string, value: unknown, onlyIfMissing?: boolean, expectedVersion?: string | null): Promise<{ value: unknown; version?: string | null }>;
}
type Entry = { adapter: PreferenceAdapter; revision: number; request: number;
 version?: string | null; pending?: { value: unknown; revision: number; version?: string | null }; running: boolean; initialized: boolean };
const clone = (value: unknown) => JSON.parse(JSON.stringify(value));
export class PreferenceSync {
 private entries = new Map<string, Entry>();
 private epoch = 0;
 private active = false;
 private ready = false;
 private bootstrap: Promise<void> | undefined;
 private bootstrapAgain = false;
 private earlyInvalidations = new Set<string>();
 private owner: string | null = null;
 private initializedKeys = new Set<string>();
 constructor(private transport: PreferenceTransport) {}
 register(key: string, adapter: PreferenceAdapter) {
  this.entries.set(key, { adapter, revision: 0, request: 0, running: false, initialized: false });
 }
 start(reset = false, owner: string | null = null) {
  this.stop(reset);
  this.owner = owner;
  this.initializedKeys.clear();
  if (owner) {
   try {
    const keys = JSON.parse(localStorage.getItem(`ai-shuffle-preference-history:${owner}`) ?? '[]');
    if (Array.isArray(keys)) this.initializedKeys = new Set(keys.filter(key => typeof key === 'string'));
   } catch { /* unavailable or malformed cache */ }
  }
  for (const [key, entry] of this.entries) entry.initialized = this.initializedKeys.has(key);
  this.active = true;
 }
 private initialized(key: string, entry: Entry) {
  entry.initialized = true;
  this.initializedKeys.add(key);
  if (this.owner) {
   try { localStorage.setItem(`ai-shuffle-preference-history:${this.owner}`, JSON.stringify([...this.initializedKeys])); }
   catch { /* persistence is best effort when browser storage is unavailable */ }
  }
 }
 stop(reset = true) {
  this.epoch++; this.active = false; this.ready = false; this.bootstrap = undefined; this.bootstrapAgain = false;
  this.earlyInvalidations.clear();
  for (const e of this.entries.values()) {
   e.pending = undefined; e.version = undefined; e.running = false; e.initialized = false; e.request++;
   if (reset) e.adapter.apply(undefined);
  }
 }
 save(key: string, value: unknown) {
  const e = this.entries.get(key);
  if (!e || !this.active) return;
  e.pending = { value: clone(value), revision: ++e.revision, version: e.version };
  void this.flush(key, e);
 }
 reconnect(): Promise<void> {
  if (this.bootstrap) { this.bootstrapAgain = true; return this.bootstrap; }
  const epoch = this.epoch;
  this.bootstrap = (async () => {
   do { this.bootstrapAgain = false; await this.snapshot(); }
   while (epoch === this.epoch && this.bootstrapAgain);
  })().finally(() => { if (epoch === this.epoch) this.bootstrap = undefined; });
  return this.bootstrap;
 }
 private async snapshot() {
  if (!this.active) return;
  const epoch = this.epoch;
  // Snapshot request revisions before awaiting: a late GET never wins over an edit.
  const versions = new Map([...this.entries].map(([k, e]) => [k, [e.revision, ++e.request]]));
  let values: Record<string, unknown>;
  let serverVersions: Record<string, string | null> | undefined;
  try {
   if (this.transport.allWithVersions) {
    const snapshot = await this.transport.allWithVersions();
    values = snapshot.preferences; serverVersions = snapshot.versions;
   } else values = await this.transport.all();
  } catch { return; }
  if (epoch !== this.epoch || !this.active) return;
  this.ready = true;
  await Promise.all([...this.entries].map(async ([key, e]) => {
   const [revision, request] = versions.get(key)!;
   if (serverVersions && e.request === request) e.version = serverVersions[key] ?? null;
   if (e.pending) { await this.flush(key, e); return; }
   if (e.revision !== revision || e.request !== request) return;
   if (Object.prototype.hasOwnProperty.call(values, key)) {
    this.initialized(key, e); e.adapter.apply(values[key]);
   } else if (!e.initialized) {
    e.initialized = true;
    e.running = true;
    try {
     const response = await this.transport.put(key, clone(e.adapter.read()), true);
     if (epoch === this.epoch && e.request === request) e.version = response.version;
     // A local edit can arrive while the migration PUT is awaiting its response.
     const pending = e.pending as Entry['pending'];
     if (epoch === this.epoch && pending) pending.version = response.version;
     if (epoch === this.epoch) this.initialized(key, e);
     if (epoch === this.epoch && e.revision === revision && !e.pending && e.request === request)
      e.adapter.apply(response.value);
    } catch { if (epoch === this.epoch) e.initialized = false; }
    finally { if (epoch === this.epoch) e.running = false; }
    if (epoch === this.epoch && e.pending) await this.flush(key, e);
   } else e.adapter.apply(undefined);
  }));
  if (epoch !== this.epoch || !this.active) return;
  const invalidated = [...this.earlyInvalidations];
  this.earlyInvalidations.clear();
  await Promise.all(invalidated.map(key => this.refresh(key)));
 }
 async refresh(key: string) {
  const e = this.entries.get(key);
  if (!e || !this.active) return;
  if (!this.ready) { this.earlyInvalidations.add(key); return; }
  if (e.pending || e.running) return;
  const epoch = this.epoch, revision = e.revision, request = ++e.request;
  try {
   const response = await this.transport.get(key);
   if (epoch === this.epoch && revision === e.revision && request === e.request && !e.pending) {
    e.version = response ? response.version : null;
    this.initialized(key, e); e.adapter.apply(response?.value);
   }
  } catch { /* reconnect retries authoritative reads, never migrates a failed GET */ }
 }
 private async flush(key: string, e: Entry) {
  if (!this.ready || !this.active || e.running || !e.pending) return;
  const epoch = this.epoch;
  e.running = true;
  try {
   while (e.pending && epoch === this.epoch) {
    const edit = e.pending;
    const expected = edit.version === undefined ? e.version : edit.version;
    const response = expected === undefined
     ? await this.transport.put(key, edit.value)
     : await this.transport.put(key, edit.value, false, expected);
    if (epoch !== this.epoch) return;
    e.version = response.version;
    this.initialized(key, e);
    if (e.pending.revision === edit.revision) e.pending = undefined;
    else e.pending.version = e.version;
   }
  } catch (error) {
   // A stale snapshot cannot overwrite another device. Reconcile conflicts;
   // retain ordinary transport failures for reconnect or the next local edit.
   if (epoch === this.epoch && (error as { status?: number }).status === 409) e.pending = undefined;
  }
  finally { if (epoch === this.epoch) e.running = false; }
  // Reconcile response/event ordering from the source of truth after the queue drains.
  if (epoch === this.epoch && !e.pending) await this.refresh(key);
 }
}
