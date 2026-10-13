/** Signed source updates (stable releases, or edge for the owner): signed metadata, local builds, atomic restart activation. */
import { createHash, randomUUID, verify } from 'node:crypto';
import { trustedKeys } from './trusted-keys';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { extractSource, type ExtractedFile } from './source-archive';
import { isLeaseValue, isLicenseKeyValue, LEASE_HEADER, LICENSE_KEY_HEADER, publicSourceFetch, SOURCE_ORIGIN } from './source-auth';
import { buildSource, removeSourceDirectory, type BuiltSource } from './source-build';
import { prepareNativeRuntime } from './source-runtime';

const REVISION = /^[a-f0-9]{40}$/;
const DIGEST = /^[a-f0-9]{64}$/;
const MAX_SIZE = 512 * 1024 ** 2;
const MAX_NOTES = 4000;
/** An update.lock without a valid pid this old is left over from a crash, not held by a running updater. */
export const STALE_LOCK_MS = 10 * 60_000;
// "0.9.0" on a release, "0.9.0-dev.12+gabc1234" on an edge build (docker/application_updates.py).
const VERSION = /^[0-9]+\.[0-9]+\.[0-9]+(?:[.+-][A-Za-z0-9.+-]{1,60})?$/;
/**
 * stable: tagged releases, signed only after the release image passed
 * container verification. nightly: daily main builds; edge is a transition
 * alias. The signed channel prevents relabeling a nightly build as stable.
 * See docs/ci/release-channels.md.
 */
export const CHANNELS = ['stable', 'nightly'] as const;
let aliasLogged = false;
export function normalizeChannel(channel: unknown): unknown {
  if (channel !== 'edge') return channel;
  if (!aliasLogged) { console.warn('Update channel edge is deprecated; using nightly'); aliasLogged = true; }
  return 'nightly';
}
export type UpdateChannel = typeof CHANNELS[number];
/** Local choice in the update root: {"channel": "nightly"}. Absent means stable. */
export const CHANNEL_FILE = 'update-channel.json';
/**
 * First-install license key in the update root: {"key": "ASH-..."}. Written
 * when the owner enters their key because the hub asked for a license before
 * any worker (and so any lease) existed; sent as X-Shuffle-License-Key only
 * while there is no lease, and removed once a lease has opened the feed.
 */
export const LICENSE_KEY_FILE = 'license-key.json';
/** The hub's gate answered 402: this install needs a lease or a license key. */
export class LicenseRequiredError extends Error {
  readonly code = 'license_required';
  constructor(readonly keySent: boolean) {
    super(keySent
      ? 'The hub did not accept this license key for updates. Check the key, or that the license is active, at hobbycoders.com.'
      : 'Updates need an active AI Shuffle license. Enter your license key, or add it in Settings > System > License.');
  }
}
const FEEDS: Record<UpdateChannel, string> = { stable: SOURCE_ORIGIN + '/v1/desktop/manifest', nightly: SOURCE_ORIGIN + '/v1/desktop/manifest/nightly' };
export interface SourceManifest {
  format: 2; revision: string; sequence: number; sha256: string; size: number;
  /** Added after format 2 shipped; older signed manifests (and installed release.json files) lack them. */
  channel?: UpdateChannel | 'edge'; version?: string; notes?: string; tree?: string;
  /** launcher: the oldest Windows launcher (installed app.asar) this release runs under (#1821 D5). Absent means any. */
  desktop?: { launcher?: number };
}
/** The launcher version a release needs; 0 for manifests signed before desktop.launcher existed. */
export function requiredLauncher(manifest: SourceManifest): number { return manifest.desktop?.launcher ?? 0; }
export interface SourceOptions {
  root: string;
  /** Overrides the update-channel.json setting; tests and owner builds only. */
  channel?: UpdateChannel | 'edge';
  publicKey: string;
  bootstrapNode: string;
  /** Trusted revision included in the installer, before its first source update. */
  baseline?: { revision: string; sequence: number };
  fetch?: typeof fetch;
  build?: (source: string, stage: string, revision: string, progress: (message: string) => void) => Promise<InstalledBuild>;
}
export interface InstalledBuild extends BuiltSource { runtimeKey: string; layout?: 2 }
export interface SourceApplication {
  revision: string;
  directory: string;
  desktopDir: string;
  serverDir: string;
  resources: string;
  python: string;
  node: string;
  electron: string;
}
/**
 * waiting: the booting revision's own process is running but has not started
 * the database yet. This attempt cannot have migrated anything; a previous
 * attempt of the same revision may still have a persistent migration fence.
 * Only builds that write it are ever treated that way; older ones never set it.
 */
export interface SourceState { schemaVersion?: 1; recoveredState?: boolean; recoveryRequired?: boolean; active?: string; previous?: string; pending?: string; staged?: string; booting?: string; waiting?: string; blocked?: boolean; failed?: string[]; mayHaveMigrated?: Record<string, number | null>; supersededFailed?: string[] }
/** state.json as every launcher reads it (desktop/LAUNCHER.md); throws on anything else. */
export function parseSourceState(text: string): SourceState {
  const state = JSON.parse(text) as SourceState;
  if (!state || typeof state !== 'object' || Array.isArray(state)) throw new Error('Invalid source update state');
  for (const field of ['active', 'previous', 'pending', 'staged', 'booting', 'waiting'] as const) {
    if (state[field] !== undefined && (typeof state[field] !== 'string' || !REVISION.test(state[field]!))) throw new Error('Invalid installed revision');
  }
  if (state.failed !== undefined && (!Array.isArray(state.failed) || state.failed.some(v => typeof v !== 'string' || !REVISION.test(v)))) throw new Error('Invalid failed revisions');
  return state;
}
export type SourceProgress = (percent: number | null, message: string) => void;
/** The signed release name and notes shown beside the update button. */
export interface ReleaseInfo { release?: string; notes?: string }
export type SourceCheck = { kind: 'current' | 'available' | 'ready'; version?: string } & ReleaseInfo;
function releaseOf(manifest: SourceManifest): ReleaseInfo {
  return { ...(manifest.version ? { release: manifest.version } : {}), ...(manifest.notes ? { notes: manifest.notes } : {}) };
}

export function verifySourceManifest(text: string, publicKey: string): SourceManifest {
  const envelope = JSON.parse(text);
  if (typeof envelope.payload !== 'string' || typeof envelope.signature !== 'string') throw new Error('Unsigned source manifest');
  const payload = Buffer.from(envelope.payload, 'base64');
  if (!trustedKeys(publicKey).some(key => verify(null, payload, key, Buffer.from(envelope.signature, 'base64')))) throw new Error('Invalid source manifest signature');
  const m = JSON.parse(payload.toString('utf8')) as SourceManifest;
  if (m.format !== 2 || typeof m.revision !== 'string' || !REVISION.test(m.revision) || typeof m.sha256 !== 'string' || !DIGEST.test(m.sha256) || !Number.isSafeInteger(m.sequence) || m.sequence <= 0 || !Number.isSafeInteger(m.size) || m.size <= 0 || m.size > MAX_SIZE) throw new Error('Invalid source manifest');
  // Optional release fields, checked as strictly as the container updater does.
  if (('channel' in m && !(CHANNELS as readonly unknown[]).includes(normalizeChannel(m.channel)))
    || ('tree' in m && !(typeof m.tree === 'string' && DIGEST.test(m.tree)))
    || ('version' in m && !(typeof m.version === 'string' && VERSION.test(m.version)))
    || ('notes' in m && !(typeof m.notes === 'string' && m.notes.length <= MAX_NOTES))
    || ('desktop' in m && !(m.desktop && typeof m.desktop === 'object' && !Array.isArray(m.desktop)
      && (!('launcher' in m.desktop) || (Number.isSafeInteger(m.desktop.launcher) && m.desktop.launcher! >= 0))))) throw new Error('Invalid source manifest release fields');
  return m;
}

/** A freshly downloaded manifest must be signed for this install's channel and carry a tree digest. */
export function requireRelease(manifest: SourceManifest, channel: UpdateChannel): SourceManifest {
  if (normalizeChannel(manifest.channel) !== normalizeChannel(channel)) throw new Error(`The update feed did not return a signed ${channel} release. No update was downloaded.`);
  if (!manifest.tree) throw new Error('This update has no signed source tree digest. No update was downloaded.');
  return manifest;
}

/**
 * The signed source tree digest (scripts/source_archive.py): SHA-256 over one
 * "path NUL sha256 LF" line per regular file below the archive root, sorted by
 * path. Python compares str by code point, which UTF-8 byte order preserves.
 */
export function treeDigest(files: ExtractedFile[]): string {
  const sorted = files.map(([file, digest]) => [Buffer.from(file, 'utf8'), digest] as const)
    .sort((a, b) => Buffer.compare(a[0], b[0]) || (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0));
  const hash = createHash('sha256');
  for (const [file, digest] of sorted) hash.update(Buffer.concat([file, Buffer.from(`\0${digest}\n`, 'utf8')]));
  return hash.digest('hex');
}

export class SourceUpdates {
  private state: SourceState = {};
  private running: string | null = null;
  // Legacy producers preserve unknown JSON fields, but always rewrite compact
  // JSON. Only our exact serialization proves the most recent state writer.
  private trackedWrite = false;
  private stateText(): string { return JSON.stringify(this.state, null, 2) + '\n'; }
  // Independent, write-ahead evidence: cached files and state.json cannot prove
  // which revision last opened PostgreSQL after a retry or a damaged state file.
  private migrationFloor: { revision: string; sequence: number; uncertain?: true } | null | undefined;
  // Highest readable marker, signed healthy revision or recorded migration.
  private recordedFloor: number | undefined;
  private async readMigrationFloor(): Promise<void> {
    this.recordedFloor = undefined;
    try {
      const floor = JSON.parse(await fs.readFile(path.join(this.options.root, 'database-floor.json'), 'utf8'));
      // Even unsupported marker formats can retain a conservative lower bound.
      if (Number.isSafeInteger(floor?.sequence) && floor.sequence > 0) this.recordedFloor = floor.sequence;
      if (!floor || floor.schemaVersion !== 1 || typeof floor.revision !== 'string' || !REVISION.test(floor.revision) ||
          !Number.isSafeInteger(floor.sequence) || floor.sequence <= 0 || (floor.uncertain !== undefined && floor.uncertain !== true)) throw new Error('Invalid database floor');
      this.migrationFloor = { revision: floor.revision, sequence: floor.sequence, ...(floor.uncertain ? { uncertain: true as const } : {}) };
    } catch (error) {
      this.migrationFloor = (error as NodeJS.ErrnoException).code === 'ENOENT' ? undefined : null;
    }
  }
  private async writeMigrationFloor(revision: string, sequence: number, uncertain?: true): Promise<void> {
    const floor = this.migrationFloor;
    if (floor && floor.sequence > sequence) throw new Error('Cannot lower the database migration floor');
    const file = path.join(this.options.root, 'database-floor.json');
    if (floor === null) await fs.rename(file, file + `.corrupt-${Date.now()}-${randomUUID()}`);
    const handle = await fs.open(file + '.tmp', 'w', 0o600);
    try { await handle.writeFile(JSON.stringify({ schemaVersion: 1, revision, sequence, ...(uncertain ? { uncertain } : {}) })); await handle.sync(); }
    finally { await handle.close(); }
    await fs.rename(file + '.tmp', file);
    // PostgreSQL must not start until the marker is durable, including the
    // directory entry. Windows does not support opening directories for fsync.
    if (process.platform !== 'win32') {
      const directory = await fs.open(this.options.root, 'r');
      try { await directory.sync(); } finally { await directory.close(); }
    }
    this.migrationFloor = { revision, sequence, ...(uncertain ? { uncertain } : {}) };
  }
  private checking = false;
  private explicitActivation = false;
  /** UI downloads must not activate on ordinary exits, including older EXE launchers. */
  stageDownloads(): void { this.explicitActivation = true; }
  async prepareInstall(): Promise<void> {
    if (!await this.updatesAllowed()) throw new Error('Application updates are disabled in Account settings');
    if (!this.state.staged) return;
    await this.assertCanRun(this.state.staged);
    this.state.pending = this.state.staged;
    delete this.state.staged;
    await this.save();
  }
  async cancelInstall(): Promise<void> {
    if (!this.explicitActivation || !this.state.pending || this.state.booting) return;
    this.state.staged = this.state.pending;
    delete this.state.pending;
    if (this.hasMigrationRisk) this.state.blocked = true;
    await this.save();
    if (this.hasMigrationRisk) throw new Error('The database may have been migrated. Install a verified newer build to recover; cancelling cannot run older code.');
  }
  /** Forget only an unselected download: never discard code that may have migrated data.
   * Files are retained; an explicit re-download rebuilds and replaces the incomplete tree. */
  async discardStaged(revision: string): Promise<boolean> {
    if (this.state.staged !== revision || this.state.active === revision || this.state.previous === revision ||
        this.state.pending === revision || this.state.failed?.includes(revision) || this.state.booting ||
        this.state.mayHaveMigrated?.[revision] !== undefined || (this.state.blocked && !this.hasMigrationRisk)) return false;
    delete this.state.staged;
    await this.save();
    return true;
  }
  get recoveredState(): boolean { return this.state.recoveredState === true; }
  get recoveryRequired(): boolean { return this.state.recoveryRequired === true; }
  get hasMigrationRisk(): boolean { return this.recoveryRequired || !!Object.keys(this.state.mayHaveMigrated ?? {}).length; }
  private async sequence(revision: string): Promise<number> {
    const manifest = verifySourceManifest(await fs.readFile(path.join(this.directory(revision), 'release.json'), 'utf8'), this.options.publicKey);
    if (manifest.revision !== revision) throw new Error('Invalid installed revision manifest');
    return manifest.sequence;
  }
  /** The migration fence survives retries, cancellation and pre-database crashes.
   * A null sequence is an unreadable legacy failure: no different revision can prove it supersedes that code. */
  private async assertCanRun(revision: string, sequence?: number): Promise<void> {
    if (this.recoveryRequired || this.migrationFloor === null) throw new Error('The database may have been migrated, but the update state cannot prove which code last used it. Install the latest AI Shuffle installer to recover.');
    if (this.migrationFloor && revision !== this.migrationFloor.revision) {
      sequence ??= await this.sequence(revision);
      if (sequence <= this.migrationFloor.sequence) throw new Error('The database may have been migrated; older code cannot run.');
    }
    if (this.state.active && revision !== this.state.active) {
      sequence ??= await this.sequence(revision);
      if (sequence <= await this.sequence(this.state.active)) throw new Error('Cannot run older code over a healthy newer application database.');
    }
    for (const [marked, migratedSequence] of Object.entries(this.state.mayHaveMigrated ?? {})) {
      if (revision === marked) continue;
      sequence ??= await this.sequence(revision);
      if (migratedSequence === null || sequence <= migratedSequence) throw new Error('The database may have been migrated. Run the affected revision or install a verified newer build; older code cannot run.');
    }
  }
  private validateState(): void {
    if (!this.state || typeof this.state !== 'object' || Array.isArray(this.state)) throw new Error('Invalid source update state');
    if (this.state.schemaVersion !== undefined && this.state.schemaVersion !== 1) throw new Error('Unsupported source update state version');
    const fields = ['schemaVersion', 'recoveredState', 'recoveryRequired', 'active', 'previous', 'pending', 'staged', 'booting', 'waiting', 'blocked', 'failed', 'mayHaveMigrated', 'supersededFailed'];
    if (Object.keys(this.state).some(field => !fields.includes(field))) throw new Error('Unknown source update state field');
    for (const field of ['recoveryRequired', 'recoveredState'] as const) if (this.state[field] !== undefined && typeof this.state[field] !== 'boolean') throw new Error('Invalid source recovery state');
    for (const field of ['active', 'previous', 'pending', 'staged', 'booting', 'waiting'] as const) {
      if (this.state[field] !== undefined && (typeof this.state[field] !== 'string' || !REVISION.test(this.state[field]!))) throw new Error('Invalid installed revision');
    }
    if (this.state.blocked !== undefined && typeof this.state.blocked !== 'boolean') throw new Error('Invalid recovery block');
    for (const field of ['failed', 'supersededFailed'] as const) {
      if (this.state[field] !== undefined && (!Array.isArray(this.state[field]) || this.state[field]!.some(v => typeof v !== 'string' || !REVISION.test(v)))) throw new Error('Invalid failed revisions');
    }
    this.validateMigrationState();
  }
  /** A legacy write cannot prove that in-flight code recorded its migrations.
   * Fence every ambiguous revision, even if it retained our schema marker. */
  private async restoreMigrationFence(): Promise<void> {
    let changed = this.state.schemaVersion === undefined;
    // Main's retry/cancel producer forgets failed and blocked. Its last write
    // makes every in-flight pointer ambiguous, even when staged or waiting.
    if (!this.trackedWrite) {
      for (const revision of new Set([this.state.pending, this.state.staged, this.state.booting, this.state.waiting])) {
        if (revision) { await this.markMigration(revision); changed = true; }
      }
    }
    if (this.state.blocked) for (const revision of this.state.failed ?? []) {
      if (this.state.mayHaveMigrated?.[revision] === undefined && !this.state.supersededFailed?.includes(revision)) { await this.markMigration(revision); changed = true; }
    }
    if (this.migrationFloor && this.state.active !== this.migrationFloor.revision &&
        !(this.migrationFloor.revision === this.options.baseline?.revision && this.migrationFloor.sequence <= this.options.baseline.sequence) &&
        this.state.mayHaveMigrated?.[this.migrationFloor.revision] === undefined) {
      let activeSequence = 0;
      if (this.state.active) try { activeSequence = await this.sequence(this.state.active); } catch { /* Keep the floor. */ }
      if (activeSequence <= this.migrationFloor.sequence) {
        this.state.mayHaveMigrated = { ...this.state.mayHaveMigrated, [this.migrationFloor.revision]: this.migrationFloor.sequence };
        changed = true;
      }
    }
    let activeSequence = 0;
    if (this.state.active) try { activeSequence = await this.sequence(this.state.active); } catch { /* Retain the other known lower bounds. */ }
    this.recordedFloor = Math.max(this.recordedFloor ?? 0, this.migrationFloor?.sequence ?? 0, activeSequence,
      ...Object.values(this.state.mayHaveMigrated ?? {}).map(sequence => sequence ?? 0));
    // Legacy healthy() keeps unknown fields but clears all first-boot pointers
    // and the block. Reconcile its completed fence, not a failed/retried boot.
    if (!this.trackedWrite && this.state.active && activeSequence > 0 &&
        !this.state.pending && !this.state.booting && !this.state.waiting && !this.state.blocked && !this.recoveryRequired &&
        activeSequence >= this.recordedFloor && (this.hasMigrationRisk || this.migrationFloor?.uncertain) &&
        Object.entries(this.state.mayHaveMigrated ?? {}).every(([revision, sequence]) =>
          revision === this.state.active || (sequence !== null && sequence < activeSequence))) {
      await this.writeMigrationFloor(this.state.active, activeSequence);
      delete this.state.mayHaveMigrated;
      this.state.supersededFailed = [...new Set([...(this.state.supersededFailed ?? []), ...(this.state.failed ?? [])])];
      changed = true;
    }
    if (Object.values(this.state.mayHaveMigrated ?? {}).some(sequence => sequence === null) && !this.recoveryRequired) {
      this.state.recoveryRequired = true; changed = true;
    }
    if (this.hasMigrationRisk && !this.state.blocked) { this.state.blocked = true; changed = true; }
    if (changed) await this.save();
  }
  private validateMigrationState(): void {
    const marked = this.state.mayHaveMigrated;
    if (marked !== undefined && (!marked || typeof marked !== 'object' || Array.isArray(marked) ||
        Object.entries(marked).some(([revision, sequence]) => !REVISION.test(revision) ||
          (sequence !== null && (!Number.isSafeInteger(sequence) || sequence <= 0))))) throw new Error('Invalid migration recovery state');
  }
  private async markMigration(revision: string): Promise<void> {
    if (this.state.mayHaveMigrated?.[revision] !== undefined) return;
    let sequence: number | null;
    try { sequence = await this.sequence(revision); } catch { sequence = null; }
    // A proven newer healthy revision has already superseded this floor.
    if (sequence !== null && this.state.active && this.state.active !== revision) {
      try { if (await this.sequence(this.state.active) > sequence) return; } catch { /* No proven supersession. */ }
    }
    this.state.mayHaveMigrated = { ...this.state.mayHaveMigrated, [revision]: sequence };
  }
  private writes: Promise<void> = Promise.resolve();
  private readonly request: typeof fetch;
  private lease: (() => Promise<string | null>) | null = null;
  constructor(readonly options: SourceOptions) {
    this.request = options.fetch ?? ((url, init) => {
      const headers = init?.headers as Record<string, string> | undefined;
      const lease = headers?.[LEASE_HEADER], key = headers?.[LICENSE_KEY_HEADER];
      return lease ? publicSourceFetch(String(url), lease) : key ? publicSourceFetch(String(url), undefined, key) : publicSourceFetch(String(url));
    });
  }
  /**
   * Where to get the license lease sent as X-Shuffle-Lease with manifest and
   * archive requests (the hub's gated feed, D8). Without it, or when it yields
   * nothing, requests go out without the header exactly as before.
   */
  useLease(provider: () => Promise<string | null>): void { this.lease = provider; }
  private async currentLease(): Promise<string | null> {
    try { const lease = await this.lease?.(); return isLeaseValue(lease) ? lease : null; }
    catch { return null; }
  }
  /** The first-install license key, or null when none was entered or the file is unreadable. */
  async licenseKey(): Promise<string | null> {
    try {
      const key = JSON.parse(await fs.readFile(path.join(this.options.root, LICENSE_KEY_FILE), 'utf8'))?.key;
      return isLicenseKeyValue(key) ? key : null;
    } catch { return null; }
  }
  /** Keep the key the owner entered for the first download; owner-only file. */
  async saveLicenseKey(key: string): Promise<void> {
    const value = key.trim();
    if (!isLicenseKeyValue(value)) throw new Error('That does not look like a license key. Keys look like ASH-XXXXX-XXXXX-XXXXX-XXXXX.');
    await fs.mkdir(this.options.root, { recursive: true });
    const file = path.join(this.options.root, LICENSE_KEY_FILE);
    await fs.writeFile(file + '.tmp', JSON.stringify({ key: value }), { mode: 0o600 });
    await fs.rename(file + '.tmp', file);
  }
  private async forgetLicenseKey(): Promise<void> {
    try { await fs.unlink(path.join(this.options.root, LICENSE_KEY_FILE)); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') console.error('Could not remove the first-install license key', error); }
  }
  get version(): string | null { return this.running ?? this.state.active ?? this.options.baseline?.revision ?? null; }
  get pendingVersion(): string | null { return this.state.staged ?? this.state.pending ?? null; }
  get hasSourceState(): boolean { return !!(this.state.active || this.state.pending || this.state.blocked || this.hasMigrationRisk); }
  /** A failed first boot with nothing newer staged: no source build may run, so the installer's bundled baseline does. */
  get blocked(): boolean { return !!(this.state.blocked || this.hasMigrationRisk) && !this.state.pending; }
  private directory(revision: string): string {
    if (!REVISION.test(revision)) throw new Error('Invalid installed revision');
    return path.join(this.options.root, 'versions', revision);
  }
  private async save(): Promise<void> {
    const file = path.join(this.options.root, 'state.json');
    this.state.schemaVersion = 1;
    const text = this.stateText();
    const write = this.writes.then(async () => { await fs.writeFile(file + '.tmp', text, { mode: 0o600 }); await fs.rename(file + '.tmp', file); });
    this.writes = write.catch(() => {}); await write;
    this.trackedWrite = true;
  }
  /** Quarantine preserves evidence, but only the independent database marker
   * proves a floor. Remaining signed files are candidates, not provenance. */
  private async recoverState(damaged: unknown): Promise<void> {
    const old = damaged && typeof damaged === 'object' && !Array.isArray(damaged) ? damaged as Record<string, unknown> : {};
    this.state = { blocked: true, recoveredState: true, mayHaveMigrated: {} };
    if (!this.migrationFloor || this.migrationFloor.uncertain) this.state.recoveryRequired = true;
    if (this.migrationFloor) this.state.mayHaveMigrated![this.migrationFloor.revision] = this.migrationFloor.sequence;
    const fences = old.mayHaveMigrated;
    if (fences !== undefined) {
      if (!fences || typeof fences !== 'object' || Array.isArray(fences)) this.state.recoveryRequired = true;
      else for (const [revision, floor] of Object.entries(fences)) {
        if (!REVISION.test(revision) || (floor !== null && (!Number.isSafeInteger(floor) || floor <= 0))) {
          this.state.recoveryRequired = true; continue;
        }
        const recorded = this.state.mayHaveMigrated![revision];
        this.state.mayHaveMigrated![revision] = floor === null || recorded === null ? null : Math.max(recorded ?? 0, floor as number);
      }
    }
    const entries = await fs.readdir(path.join(this.options.root, 'versions'), { withFileTypes: true });
    // Do not perform unbounded signature/build probes on a damaged cache.
    if (entries.length > 1024) this.state.recoveryRequired = true;
    let newest: { revision: string; sequence: number } | undefined;
    let tied = false;
    if (!this.recoveryRequired) for (const entry of entries) {
      if (!REVISION.test(entry.name)) continue;
      if (!entry.isDirectory() || entry.isSymbolicLink()) { this.state.recoveryRequired = true; break; }
      let sequence: number;
      try { sequence = await this.sequence(entry.name); } catch { continue; }
      if (!newest || sequence > newest.sequence) { newest = { revision: entry.name, sequence }; tied = false; }
      else if (sequence === newest.sequence) tied = true;
    }
    if (newest && !tied && !this.recoveryRequired &&
        Object.values(this.state.mayHaveMigrated!).every(floor => floor !== null && floor <= newest!.sequence)) {
      try { await this.assertCanRun(newest.revision, newest.sequence); }
      catch { this.state.recoveryRequired = true; }
      if (!this.recoveryRequired) try {
        await this.application(newest.revision);
        this.state.pending = newest.revision;
      } catch { /* A newer installer can replace an incomplete build. */ }
    } else this.state.recoveryRequired = true;
    await this.save();
  }
  async load(): Promise<void> {
    await fs.mkdir(path.join(this.options.root, 'versions'), { recursive: true });
    await this.readMigrationFloor();
    const file = path.join(this.options.root, 'state.json');
    let text: string | undefined;
    try { text = await fs.readFile(file, 'utf8'); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
    if (text !== undefined) {
      let damaged: unknown;
      try {
        damaged = JSON.parse(text); this.state = damaged as SourceState; this.validateState();
        this.trackedWrite = text === this.stateText();
      }
      catch {
        await fs.rename(file, file + `.corrupt-${Date.now()}-${randomUUID()}`);
        await this.recoverState(damaged);
      }
    } else if ((await fs.readdir(this.options.root)).some(name => name.startsWith('state.json.corrupt-'))) {
      // A crash between quarantine and save must not erase the recovery block.
      await this.recoverState(undefined);
    }
    if (this.migrationFloor === null) { this.state.recoveryRequired = true; this.state.blocked = true; await this.save(); }
    await this.restoreMigrationFence();
    // A crashed first boot may already have migrated the database. Never roll
    // application code back automatically over that possibly changed database.
    // One that crashed before it started the database changed nothing: the
    // previous version runs again and the update stays ready to retry.
    if (this.state.booting && this.state.waiting === this.state.booting) await this.unstarted();
    else if (this.state.booting) await this.failed();
    // Policy may discard a fresh download, but never the code needed to finish
    // a possibly migrated database. That revision can recover with updates off.
    if (this.state.pending && !this.hasMigrationRisk && !await this.updatesAllowed()) {
      delete this.state.pending;
      await this.save();
    }
  }
  /** The release channel this install follows: stable unless the owner opted into nightly. */
  async channel(): Promise<UpdateChannel> {
    if (this.options.channel) return normalizeChannel(this.options.channel) as UpdateChannel;
    let text: string;
    try { text = await fs.readFile(path.join(this.options.root, CHANNEL_FILE), 'utf8'); }
    catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return 'stable'; throw error; }
    let channel: unknown;
    try { channel = normalizeChannel(JSON.parse(text)?.channel); } catch { channel = undefined; }
    if (!(CHANNELS as readonly unknown[]).includes(channel)) throw new Error(`${CHANNEL_FILE} must contain {"channel": "stable"} or {"channel": "nightly"}. No update was checked.`);
    return channel as UpdateChannel;
  }
  async channelConfig(): Promise<{ channel: UpdateChannel; source: 'file' | 'default'; editable: boolean }> {
    const channel = await this.channel();
    const exists = await fs.stat(path.join(this.options.root, CHANNEL_FILE)).then(() => true, error => {
      if (error.code === 'ENOENT') return false; throw error;
    });
    return { channel, source: exists ? 'file' : 'default', editable: !this.options.channel };
  }
  async setChannel(channel: UpdateChannel): Promise<void> {
    if (!(CHANNELS as readonly string[]).includes(channel) || this.options.channel) throw new Error('Channel cannot be changed');
    if (await this.channel() === 'nightly' && channel === 'stable') throw new Error('Returning to stable requires a reinstall');
    if (this.pendingVersion || this.hasMigrationRisk) throw new Error('Finish the current update before changing channel');
    await fs.mkdir(this.options.root, { recursive: true });
    const file = path.join(this.options.root, CHANNEL_FILE);
    await fs.writeFile(file + '.tmp', JSON.stringify({ channel }), { mode: 0o600 });
    await fs.rename(file + '.tmp', file);
  }
  private async updatesAllowed(): Promise<boolean> {
    try {
      const policy = JSON.parse(await fs.readFile(path.join(this.options.root, 'allow-updates.json'), 'utf8'));
      return policy.allow_updates === true;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return true;
      throw new Error('Cannot read application update policy');
    }
  }
  /** A newer milestone installer may supersede older healthy source code, and a block left by older failed boots. */
  get canConfirmBaselineRecovery(): boolean {
    const baseline = this.options.baseline;
    return !!baseline && this.recoveryRequired && !this.state.booting &&
      (this.recordedFloor === undefined || baseline.sequence >= this.recordedFloor) &&
      (!Object.values(this.state.mayHaveMigrated ?? {}).includes(null) || baseline.sequence > (this.recordedFloor ?? 0)) &&
      (!this.migrationFloor || baseline.sequence >= this.migrationFloor.sequence) &&
      Object.values(this.state.mayHaveMigrated ?? {}).every(floor => floor === null || baseline.sequence >= floor);
  }
  async useNewerBaseline(ownerConfirmed = false): Promise<void> {
    const baseline = this.options.baseline;
    if (!baseline || this.state.booting || baseline.sequence < (this.recordedFloor ?? 0) ||
        (Object.values(this.state.mayHaveMigrated ?? {}).includes(null) && baseline.sequence <= (this.recordedFloor ?? 0))) return;
    if (this.recoveryRequired || (this.state.recoveredState && this.state.blocked)) {
      const proven = !!this.migrationFloor && !this.migrationFloor.uncertain && baseline.sequence >= this.migrationFloor.sequence &&
        Object.values(this.state.mayHaveMigrated ?? {}).every(floor => floor !== null && baseline.sequence >= floor);
      if (!proven && !(ownerConfirmed && this.canConfirmBaselineRecovery)) return;
      await this.finishBaselineRecovery();
      return;
    }
    // The signed sequence captured before database startup remains available
    // even if the failed build's files have since become unreadable.
    if (Object.values(this.state.mayHaveMigrated ?? {}).some(sequence => sequence === null || sequence >= baseline.sequence)) return;
    const revisions = [this.state.active, this.state.pending, this.state.staged].filter((value): value is string => !!value);
    if (!revisions.length && !this.state.blocked && !this.hasMigrationRisk) return;
    for (const revision of revisions) {
      if (this.state.mayHaveMigrated?.[revision] !== undefined) continue; // Proven older than the installer above.
      let manifest: SourceManifest;
      try { manifest = verifySourceManifest(await fs.readFile(path.join(this.directory(revision), 'release.json'), 'utf8'), this.options.publicKey); }
      catch {
        if (await this.discardStaged(revision)) continue;
        await this.markMigration(revision);
        this.state.recoveryRequired = true; this.state.blocked = true;
        await this.save(); return;
      }
      if (manifest.sequence >= baseline.sequence) return;
    }
    if (this.state.blocked) {
      // A failed revision may have migrated the database; only an installer
      // newer than every failed revision may take over. Unreadable metadata
      // keeps the block: neither the failed build nor an older baseline runs.
      for (const revision of this.state.failed ?? []) {
        if (this.state.mayHaveMigrated?.[revision] !== undefined) continue;
        let sequence: number;
        try { sequence = verifySourceManifest(await fs.readFile(path.join(this.directory(revision), 'release.json'), 'utf8'), this.options.publicKey).sequence; }
        catch {
          await this.markMigration(revision);
          this.state.recoveryRequired = true; this.state.blocked = true;
          await this.save(); return;
        }
        if (sequence >= baseline.sequence) return;
      }
    }
    await this.finishBaselineRecovery();
  }
  private async finishBaselineRecovery(): Promise<void> {
    const baseline = this.options.baseline!;
    // Write ahead of clearing the block: a crash cannot erase the known floor.
    await this.writeMigrationFloor(baseline.revision, baseline.sequence);
    this.state.supersededFailed = [...new Set([...(this.state.supersededFailed ?? []), ...(this.state.failed ?? [])])];
    this.state.previous = this.state.active ?? this.state.previous;
    delete this.state.active; delete this.state.pending; delete this.state.staged; delete this.state.blocked; delete this.state.mayHaveMigrated;
    delete this.state.recoveryRequired; delete this.state.recoveredState;
    await this.save();
  }
  private async lock(): Promise<() => Promise<void>> {
    const file = path.join(this.options.root, 'update.lock');
    try { const handle = await fs.open(file, 'wx'); await handle.writeFile(String(process.pid)); await handle.close(); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
      const text = await fs.readFile(file, 'utf8');
      const pid = /^\s*\d+\s*$/.test(text) ? Number(text) : NaN;
      if (!Number.isSafeInteger(pid) || pid <= 0) {
        // A crash between creating the lock and writing its pid leaves it empty.
        // A fresh one may belong to an updater that is still writing it; once
        // it is old enough no live updater can own it, so it is taken over.
        const age = Date.now() - (await fs.stat(file)).mtimeMs;
        if (age < STALE_LOCK_MS) throw new Error('An update lock needs attention; no files were changed');
        console.warn(`Taking over an update lock without a process id, last changed ${Math.round(age / 60_000)} min ago: ${file}`);
        await fs.unlink(file); return this.lock();
      }
      try { process.kill(pid, 0); throw new Error('An application update is already running'); }
      catch (failure) { if ((failure as NodeJS.ErrnoException).code !== 'ESRCH') throw failure; }
      await fs.unlink(file); return this.lock();
    }
    return () => fs.unlink(file);
  }
  private async response(url: string): Promise<Response> {
    // Only hub update URLs reach here (FEEDS and the hub's /v1/desktop/sources/<revision>).
    if (!url.startsWith(SOURCE_ORIGIN + '/v1/desktop/')) throw new Error('Invalid source download endpoint');
    // A lease from the worker wins; the first-install key is only for before one exists.
    const lease = await this.currentLease();
    const key = lease ? null : await this.licenseKey();
    const headers: Record<string, string> = { 'User-Agent': 'AI-Shuffle-Source-Updater', ...(lease ? { [LEASE_HEADER]: lease } : key ? { [LICENSE_KEY_HEADER]: key } : {}) };
    const response = await this.request(url, { signal: AbortSignal.timeout(120_000), headers });
    if (response.status === 402) throw new LicenseRequiredError(!!key);
    if (!response.ok || !response.body) throw new Error(`Source update request failed (${response.status})`);
    // The worker has a lease and the hub took it: the first-install key has done its job.
    if (lease) await this.forgetLicenseKey();
    return response;
  }
  private async manifest(): Promise<{ signed: string; manifest: SourceManifest }> {
    const channel = await this.channel();
    const response = await this.response(FEEDS[channel]);
    const chunks: Uint8Array[] = []; let size = 0;
    for await (const chunk of response.body as unknown as AsyncIterable<Uint8Array>) {
      if ((size += chunk.length) > 32 * 1024) throw new Error('Source manifest exceeds limits'); chunks.push(chunk);
    }
    const signed = Buffer.concat(chunks).toString('utf8');
    return { signed, manifest: requireRelease(verifySourceManifest(signed, this.options.publicKey), channel) };
  }
  /** Release name and notes of a downloaded revision, from its stored signed manifest; empty when unreadable. */
  async releaseInfo(revision: string): Promise<ReleaseInfo> {
    try { return releaseOf(verifySourceManifest(await fs.readFile(path.join(this.directory(revision), 'release.json'), 'utf8'), this.options.publicKey)); }
    catch { return {}; }
  }
  async check(_progress: SourceProgress = () => {}, interactive = false): Promise<SourceCheck> {
    return this.update(false, () => {}, interactive);
  }
  async download(progress: SourceProgress = () => {}, interactive = true): Promise<SourceCheck> {
    return this.update(true, progress, interactive);
  }
  private async update(download: boolean, progress: SourceProgress, interactive: boolean): Promise<SourceCheck> {
    if (!await this.updatesAllowed()) throw new Error('Application updates are disabled in Account settings');
    if (this.checking) throw new Error('An application update is already running');
    if (this.state.booting) return { kind: 'current' };
    if (this.pendingVersion) return { kind: 'ready', version: this.pendingVersion.slice(0, 12), ...await this.releaseInfo(this.pendingVersion) };
    this.checking = true;
    let unlock: (() => Promise<void>) | undefined;
    try {
      unlock = await this.lock();
      const { signed, manifest } = await this.manifest();
      const installed = this.state.pending ?? this.version;
      if (manifest.revision === installed) return { kind: this.state.pending ? 'ready' : 'current', version: this.state.pending?.slice(0, 12) };
      if (installed) {
        const old = installed === this.options.baseline?.revision && !this.state.active
          ? this.options.baseline
          : verifySourceManifest(await fs.readFile(path.join(this.directory(installed), 'release.json'), 'utf8'), this.options.publicKey);
        if (manifest.sequence <= old.sequence) return { kind: 'current' };
      }
      if (this.state.failed?.includes(manifest.revision)) throw new Error('This revision failed to start. Check again after a newer update. Previous files and data have been retained.');
      if (!download) return { kind: 'available', version: manifest.revision.slice(0, 12), ...releaseOf(manifest) };
      await this.stage(manifest, signed, progress);
      return { kind: 'ready', version: manifest.revision.slice(0, 12), ...releaseOf(manifest) };
    } finally { try { if (unlock) await unlock(); } finally { this.checking = false; } }
  }
  private async stage(manifest: SourceManifest, signed: string, progress: SourceProgress): Promise<void> {
    await this.assertCanRun(manifest.revision, manifest.sequence);
    const stage = path.join(this.options.root, 'staging');
    await removeSourceDirectory(stage);
    await fs.mkdir(stage, { recursive: true });
    try {
      progress(0, 'Downloading verified source…');
      const response = await this.response(`${SOURCE_ORIGIN}/v1/desktop/sources/${manifest.revision}`);
      const archive = path.join(stage, 'source.zip');
      const file = await fs.open(archive, 'wx'); const hash = createHash('sha256'); let size = 0;
      try {
        for await (const chunk of response.body as unknown as AsyncIterable<Uint8Array>) {
          size += chunk.length; if (size > manifest.size) throw new Error('Source archive exceeds signed size');
          hash.update(chunk); await file.writeFile(chunk); progress(Math.floor(size / manifest.size * 100), 'Downloading verified source…');
        }
      } finally { await file.close(); }
      if (size !== manifest.size || hash.digest('hex') !== manifest.sha256) throw new Error('Source archive checksum or size mismatch');
      const unpacked = path.join(stage, 'unpacked'); const extracted = await extractSource(archive, unpacked);
      const entries = await fs.readdir(unpacked);
      if (entries.length !== 1 || !new RegExp(`^(ai-shuffle-${manifest.revision}|HobbyCoders-ai-shuffle-${manifest.revision.slice(0, 7)}[a-f0-9]*)$`, "i").test(entries[0])) throw new Error('Unexpected source archive root');
      // Recompute the signed tree digest over what was written, before any of it is built or run.
      const prefix = entries[0] + '/';
      if (extracted.some(([file]) => !file.startsWith(prefix)) || treeDigest(extracted.map(([file, digest]) => [file.slice(prefix.length), digest])) !== manifest.tree) {
        throw new Error('Source files do not match the signed release. The update was not built; the running version is unchanged.');
      }
      await fs.rename(path.join(unpacked, entries[0]), path.join(stage, 'source'));
      await fs.rmdir(unpacked); await fs.unlink(archive);
      const source = path.join(stage, 'source');
      const message = (text: string) => progress(null, text);
      let built: InstalledBuild;
      if (this.options.build) built = await this.options.build(source, stage, manifest.revision, message);
      else {
        const native = await prepareNativeRuntime(source, this.options.root, this.options.bootstrapNode, message);
        built = { ...await buildSource({ source, server: path.join(stage, 'server'), cache: path.join(this.options.root, 'dependencies', native.key.slice(0, 24)), runtime: native.runtime, revision: manifest.revision, log: path.join(this.options.root, 'build.log'), progress: message }), runtimeKey: native.key, layout: 2 };
      }
      await fs.writeFile(path.join(stage, 'build.json'), JSON.stringify(built));
      await fs.writeFile(path.join(stage, 'release.json'), signed);
      const target = this.directory(manifest.revision);
      if ([this.state.active, this.state.previous].includes(manifest.revision)) throw new Error('Cannot overwrite a retained application version');
      await removeSourceDirectory(target); await fs.rename(stage, target);
      if (!await this.updatesAllowed()) throw new Error('Updates were disabled while preparing; no update was activated');
      const field = this.explicitActivation ? 'staged' : 'pending';
      this.state[field] = manifest.revision;
      try { await this.save(); } catch (error) { delete this.state[field]; throw error; }
    } finally { await removeSourceDirectory(stage); }
  }
  /** The signed manifest an installed revision was built from, verified again. */
  async release(revision: string): Promise<SourceManifest> {
    const manifest = verifySourceManifest(await fs.readFile(path.join(this.directory(revision), 'release.json'), 'utf8'), this.options.publicKey);
    if (manifest.revision !== revision) throw new Error('Invalid built application');
    return manifest;
  }
  async application(revision: string): Promise<SourceApplication> {
    const dir = this.directory(revision);
    const manifest = verifySourceManifest(await fs.readFile(path.join(dir, 'release.json'), 'utf8'), this.options.publicKey);
    const built = JSON.parse(await fs.readFile(path.join(dir, 'build.json'), 'utf8')) as InstalledBuild;
    if (manifest.revision !== revision || built.revision !== revision || !DIGEST.test(built.runtimeKey) || (built.pythonKey !== null && !DIGEST.test(built.pythonKey)) || (built.layout !== undefined && built.layout !== 2)) throw new Error('Invalid built application');
    const cacheKey = built.layout === 2 ? built.runtimeKey.slice(0, 24) : built.runtimeKey;
    const resources = path.join(this.options.root, 'runtimes', cacheKey);
    const windows = process.platform === 'win32';
    const python = built.pythonKey ? path.join(this.options.root, 'dependencies', cacheKey, 'python', built.pythonKey.slice(0, 24), windows ? 'Scripts/python.exe' : 'bin/python') : path.join(resources, windows ? 'worker/python/python.exe' : 'worker/python/bin/python3');
    const desktopDir = path.join(dir, 'source/desktop');
    const electron = path.resolve(built.electron);
    const dependencyRoot = path.resolve(this.options.root, 'dependencies') + path.sep;
    if (!(windows ? electron.toLowerCase().startsWith(dependencyRoot.toLowerCase()) : electron.startsWith(dependencyRoot))) throw new Error('Desktop runtime is outside the managed dependency cache');
    for (const file of [path.join(desktopDir, 'dist/main/managed-entry.js'), path.join(dir, 'server/app/main.py'), path.join(dir, 'server/app/static/index.html'), python, electron]) {
      if (!(await fs.stat(file)).isFile()) throw new Error('Application build is incomplete');
    }
    return { revision, directory: dir, desktopDir, serverDir: path.join(dir, 'server'), resources, python, node: path.join(resources, windows ? 'worker/node/node.exe' : 'worker/node/bin/node'), electron };
  }
  async select(preflight?: (application: SourceApplication) => Promise<void>): Promise<SourceApplication> {
    const revision = this.state.pending ?? this.state.active;
    if (!revision || this.blocked) throw new Error('No healthy source build is available. Install the latest AI Shuffle installer to recover. Previous files and data have been retained.');
    await this.assertCanRun(revision);
    const application = await this.application(revision);
    // Compatibility failures must not become failed boots or block the next launch.
    await preflight?.(application);
    if (this.state.pending) {
      // Delegated older producers may not write the database marker at all.
      // Invalidate its provenance before handing them newer code; only a new
      // databaseStarting producer can establish the next durable floor.
      if (this.migrationFloor && this.migrationFloor.revision !== revision) {
        await this.writeMigrationFloor(this.migrationFloor.revision, this.migrationFloor.sequence, true);
      }
      this.state.booting = revision; delete this.state.waiting; await this.save();
    }
    this.running = revision; return application;
  }
  /** Attach from the newly launched Electron process without interpreting its own first boot as a crash. */
  async attach(revision: string): Promise<void> {
    const text = await fs.readFile(path.join(this.options.root, 'state.json'), 'utf8');
    this.state = JSON.parse(text);
    this.validateState();
    this.trackedWrite = text === this.stateText();
    await this.readMigrationFloor();
    await this.restoreMigrationFence();
    if (![this.state.active, this.state.booting].includes(revision)) throw new Error('Unexpected desktop launch revision');
    await this.assertCanRun(revision);
    await this.application(revision); this.running = revision;
    if (this.state.booting === revision) { this.state.waiting = revision; await this.save(); }
  }
  /** Called just before this launch starts the database: from here a crashed first boot must block. */
  async databaseStarting(): Promise<void> {
    const revision = this.running ?? this.state.active ?? this.options.baseline?.revision;
    if (!revision) return;
    const sequence = this.running || this.state.active ? await this.sequence(revision) : this.options.baseline!.sequence;
    await this.assertCanRun(revision, sequence);
    await this.writeMigrationFloor(revision, sequence);
    if (this.state.booting !== this.running || !this.running) return;
    this.state.supersededFailed = this.state.supersededFailed?.filter(revision => revision !== this.running);
    this.state.mayHaveMigrated = { ...this.state.mayHaveMigrated,
      [this.running]: Math.max(this.state.mayHaveMigrated?.[this.running] ?? 0, sequence) };
    this.state.blocked = true;
    delete this.state.waiting; await this.save();
  }
  async healthy(): Promise<void> {
    if (!this.state.booting || this.state.booting !== this.running) return;
    await this.assertCanRun(this.running);
    const sequence = await this.sequence(this.running);
    for (const [revision, migratedSequence] of Object.entries(this.state.mayHaveMigrated ?? {})) {
      if (revision === this.running || (migratedSequence !== null && sequence > migratedSequence)) delete this.state.mayHaveMigrated![revision];
    }
    if (!this.hasMigrationRisk) delete this.state.mayHaveMigrated;
    this.state.previous = this.state.active; this.state.active = this.running;
    // Preserve failed builds for diagnostics, but do not resurrect their fences
    // after a verified healthy supersession, even if their old files disappear.
    this.state.supersededFailed = [...new Set([...(this.state.supersededFailed ?? []), ...(this.state.failed ?? [])])];
    delete this.state.pending; delete this.state.booting; delete this.state.waiting; delete this.state.blocked; delete this.state.recoveredState; await this.save();
    const node = (await this.application(this.running)).node;
    await fs.writeFile(path.join(this.options.root, 'node-path.txt.tmp'), node);
    await fs.rename(path.join(this.options.root, 'node-path.txt.tmp'), path.join(this.options.root, 'node-path.txt'));
    // Keep all previous native runtimes: PostgreSQL data may still need an old
    // major version. Only prune application files after a healthy new startup.
    const keep = new Set([this.state.active, this.state.previous, ...(this.state.failed ?? []), ...Object.keys(this.state.mayHaveMigrated ?? {})]);
    for (const entry of await fs.readdir(path.join(this.options.root, 'versions'), { withFileTypes: true })) {
      if (entry.isDirectory() && !entry.isSymbolicLink() && REVISION.test(entry.name) && !keep.has(entry.name)) await removeSourceDirectory(this.directory(entry.name));
    }
  }
  /**
   * A quit before this first boot started the database (the first-run
   * disclosure was declined or never answered) cannot have migrated anything:
   * the next launch tries the same build again instead of blocking it.
   */
  async deferBoot(): Promise<void> {
    if (!this.running || this.state.booting !== this.running) return;
    delete this.state.booting; delete this.state.waiting; await this.save();
  }
  /** This attempt changed nothing. A prior migration fence still blocks older code. */
  private async unstarted(): Promise<void> {
    this.state.staged = this.state.booting;
    delete this.state.booting; delete this.state.waiting; delete this.state.pending; await this.save();
  }
  async failed(): Promise<void> {
    if (!this.state.booting) return;
    if (this.state.waiting === this.state.booting) { await this.unstarted(); return; }
    this.state.supersededFailed = this.state.supersededFailed?.filter(revision => revision !== this.state.booting);
    await this.markMigration(this.state.booting);
    this.state.failed = [...new Set([...(this.state.failed ?? []), this.state.booting])];
    delete this.state.booting; delete this.state.waiting; delete this.state.pending; this.state.blocked = true; await this.save();
  }
  /**
   * Blocked by a failed first boot: run the newest failed build again. It is
   * the newest code that may have changed the database, so this is never a
   * rollback. False when no failed build is left to run.
   */
  async retryFailed(): Promise<boolean> {
    if (!this.blocked) return false;
    let newest: { revision: string; sequence: number } | null = null;
    for (const revision of new Set([...(this.state.failed ?? []), ...Object.keys(this.state.mayHaveMigrated ?? {})])) {
      try {
        await this.assertCanRun(revision);
        const { sequence } = verifySourceManifest(await fs.readFile(path.join(this.directory(revision), 'release.json'), 'utf8'), this.options.publicKey);
        await this.application(revision);
        if (!newest || sequence > newest.sequence) newest = { revision, sequence };
      } catch { /* Missing or unreadable files: that build cannot run again. */ }
    }
    if (!newest) return false;
    this.state.failed = (this.state.failed ?? []).filter(value => value !== newest.revision);
    if (!this.state.failed.length) delete this.state.failed;
    if (this.hasMigrationRisk) this.state.blocked = true; else delete this.state.blocked;
    this.state.pending = newest.revision;
    if (this.state.staged === newest.revision) delete this.state.staged;
    await this.save();
    return true;
  }
}
