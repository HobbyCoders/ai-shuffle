/** Main-branch source updates: authenticated metadata, local builds, atomic restart activation. */
import { createHash, verify } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { extractSource } from './source-archive';
import { accountSourceFetch, SOURCE_ORIGIN } from './source-auth';
import { buildSource, removeSourceDirectory, type BuiltSource } from './source-build';
import { prepareNativeRuntime } from './source-runtime';

const REVISION = /^[a-f0-9]{40}$/;
const DIGEST = /^[a-f0-9]{64}$/;
const FEED = SOURCE_ORIGIN + '/v1/desktop/manifest';
const MAX_SIZE = 512 * 1024 ** 2;
export interface SourceManifest { format: 2; revision: string; sequence: number; sha256: string; size: number }
export interface SourceOptions {
  root: string;
  publicKey: string;
  bootstrapNode: string;
  fetch?: typeof fetch;
  build?: (source: string, stage: string, revision: string, progress: (message: string) => void) => Promise<InstalledBuild>;
}
export interface InstalledBuild extends BuiltSource { runtimeKey: string }
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
interface SourceState { active?: string; previous?: string; pending?: string; booting?: string; blocked?: boolean; failed?: string[] }
export type SourceProgress = (percent: number | null, message: string) => void;
export type SourceCheck = { kind: 'current' | 'available' | 'ready'; version?: string };

export function verifySourceManifest(text: string, publicKey: string): SourceManifest {
  const envelope = JSON.parse(text);
  if (typeof envelope.payload !== 'string' || typeof envelope.signature !== 'string') throw new Error('Unsigned source manifest');
  const payload = Buffer.from(envelope.payload, 'base64');
  if (!verify(null, payload, publicKey, Buffer.from(envelope.signature, 'base64'))) throw new Error('Invalid source manifest signature');
  const m = JSON.parse(payload.toString('utf8')) as SourceManifest;
  if (m.format !== 2 || typeof m.revision !== 'string' || !REVISION.test(m.revision) || typeof m.sha256 !== 'string' || !DIGEST.test(m.sha256) || !Number.isSafeInteger(m.sequence) || m.sequence <= 0 || !Number.isSafeInteger(m.size) || m.size <= 0 || m.size > MAX_SIZE) throw new Error('Invalid source manifest');
  return m;
}

export class SourceUpdates {
  private state: SourceState = {};
  private running: string | null = null;
  private checking = false;
  private writes: Promise<void> = Promise.resolve();
  private readonly request: typeof fetch;
  private interactive = false;
  constructor(readonly options: SourceOptions) { this.request = options.fetch ?? ((url) => accountSourceFetch(options.root, String(url), this.interactive)); }
  get version(): string | null { return this.running ?? this.state.active ?? null; }
  get pendingVersion(): string | null { return this.state.pending ?? null; }
  private directory(revision: string): string {
    if (!REVISION.test(revision)) throw new Error('Invalid installed revision');
    return path.join(this.options.root, 'versions', revision);
  }
  private async save(): Promise<void> {
    const file = path.join(this.options.root, 'state.json');
    const text = JSON.stringify(this.state);
    const write = this.writes.then(async () => { await fs.writeFile(file + '.tmp', text, { mode: 0o600 }); await fs.rename(file + '.tmp', file); });
    this.writes = write.catch(() => {}); await write;
  }
  async load(): Promise<void> {
    await fs.mkdir(path.join(this.options.root, 'versions'), { recursive: true });
    try {
      this.state = JSON.parse(await fs.readFile(path.join(this.options.root, 'state.json'), 'utf8'));
      if (!this.state || typeof this.state !== 'object' || Array.isArray(this.state)) throw new Error('Invalid source update state');
      for (const field of ['active', 'previous', 'pending', 'booting'] as const) {
        if (this.state[field] !== undefined && (typeof this.state[field] !== 'string' || !REVISION.test(this.state[field]!))) throw new Error('Invalid installed revision');
      }
      if (this.state.failed !== undefined && (!Array.isArray(this.state.failed) || this.state.failed.some(v => typeof v !== 'string' || !REVISION.test(v)))) throw new Error('Invalid failed revisions');
    } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
    // A crashed first boot may already have migrated the database. Never roll
    // application code back automatically over that possibly changed database.
    if (this.state.booting) await this.failed();
  }
  private async lock(): Promise<() => Promise<void>> {
    const file = path.join(this.options.root, 'update.lock');
    try { const handle = await fs.open(file, 'wx'); await handle.writeFile(String(process.pid)); await handle.close(); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
      const pid = Number(await fs.readFile(file, 'utf8'));
      if (!Number.isSafeInteger(pid) || pid <= 0) throw new Error('An update lock needs attention; no files were changed');
      try { process.kill(pid, 0); throw new Error('An application update is already running'); }
      catch (failure) { if ((failure as NodeJS.ErrnoException).code !== 'ESRCH') throw failure; }
      await fs.unlink(file); return this.lock();
    }
    return () => fs.unlink(file);
  }
  private async response(url: string): Promise<Response> {
    const response = await this.request(url, { signal: AbortSignal.timeout(120_000), headers: { 'User-Agent': 'AI-Shuffle-Source-Updater' } });
    if (!response.ok || !response.body) throw new Error(`Source update request failed (${response.status})`);
    return response;
  }
  private async manifest(): Promise<{ signed: string; manifest: SourceManifest }> {
    const response = await this.response(FEED);
    const chunks: Uint8Array[] = []; let size = 0;
    for await (const chunk of response.body as unknown as AsyncIterable<Uint8Array>) {
      if ((size += chunk.length) > 32 * 1024) throw new Error('Source manifest exceeds limits'); chunks.push(chunk);
    }
    const signed = Buffer.concat(chunks).toString('utf8');
    return { signed, manifest: verifySourceManifest(signed, this.options.publicKey) };
  }
  async check(_progress: SourceProgress = () => {}, interactive = false): Promise<SourceCheck> {
    return this.update(false, () => {}, interactive);
  }
  async download(progress: SourceProgress = () => {}, interactive = true): Promise<SourceCheck> {
    return this.update(true, progress, interactive);
  }
  private async update(download: boolean, progress: SourceProgress, interactive: boolean): Promise<SourceCheck> {
    if (this.checking) throw new Error('An application update is already running');
    if (this.state.booting) return { kind: 'current' };
    if (this.state.pending) return { kind: 'ready', version: this.state.pending.slice(0, 12) };
    this.checking = true;
    this.interactive = interactive;
    let unlock: (() => Promise<void>) | undefined;
    try {
      unlock = await this.lock();
      const { signed, manifest } = await this.manifest();
      const installed = this.state.pending ?? this.version;
      if (manifest.revision === installed) return { kind: this.state.pending ? 'ready' : 'current', version: this.state.pending?.slice(0, 12) };
      if (installed) {
        const old = verifySourceManifest(await fs.readFile(path.join(this.directory(installed), 'release.json'), 'utf8'), this.options.publicKey);
        if (manifest.sequence <= old.sequence) return { kind: 'current' };
      }
      if (this.state.failed?.includes(manifest.revision)) throw new Error('This revision failed to start. Check again after a newer main-branch update. Previous files and data have been retained.');
      if (!download) return { kind: 'available', version: manifest.revision.slice(0, 12) };
      await this.stage(manifest, signed, progress);
      return { kind: 'ready', version: manifest.revision.slice(0, 12) };
    } finally { try { if (unlock) await unlock(); } finally { this.checking = false; this.interactive = false; } }
  }
  private async stage(manifest: SourceManifest, signed: string, progress: SourceProgress): Promise<void> {
    const stage = path.join(this.options.root, 'staging');
    await removeSourceDirectory(stage);
    await fs.mkdir(stage, { recursive: true });
    try {
      progress(0, 'Downloading source from main…');
      const response = await this.response(`${SOURCE_ORIGIN}/v1/desktop/sources/${manifest.revision}`);
      const archive = path.join(stage, 'source.zip');
      const file = await fs.open(archive, 'wx'); const hash = createHash('sha256'); let size = 0;
      try {
        for await (const chunk of response.body as unknown as AsyncIterable<Uint8Array>) {
          size += chunk.length; if (size > manifest.size) throw new Error('Source archive exceeds signed size');
          hash.update(chunk); await file.writeFile(chunk); progress(Math.floor(size / manifest.size * 100), 'Downloading source from main…');
        }
      } finally { await file.close(); }
      if (size !== manifest.size || hash.digest('hex') !== manifest.sha256) throw new Error('Source archive checksum or size mismatch');
      const unpacked = path.join(stage, 'unpacked'); await extractSource(archive, unpacked);
      const entries = await fs.readdir(unpacked);
      if (entries.length !== 1 || !new RegExp(`^(ai-shuffle-${manifest.revision}|HobbyCoders-ai-shuffle-${manifest.revision.slice(0, 7)}[a-f0-9]*)$`, "i").test(entries[0])) throw new Error('Unexpected source archive root');
      await fs.rename(path.join(unpacked, entries[0]), path.join(stage, 'source'));
      await fs.rmdir(unpacked); await fs.unlink(archive);
      const source = path.join(stage, 'source');
      const message = (text: string) => progress(null, text);
      let built: InstalledBuild;
      if (this.options.build) built = await this.options.build(source, stage, manifest.revision, message);
      else {
        const native = await prepareNativeRuntime(source, this.options.root, this.options.bootstrapNode, message);
        built = { ...await buildSource({ source, server: path.join(stage, 'server'), cache: path.join(this.options.root, 'dependencies', native.key), runtime: native.runtime, revision: manifest.revision, log: path.join(this.options.root, 'build.log'), progress: message }), runtimeKey: native.key };
      }
      await fs.writeFile(path.join(stage, 'build.json'), JSON.stringify(built));
      await fs.writeFile(path.join(stage, 'release.json'), signed);
      const target = this.directory(manifest.revision);
      if ([this.state.active, this.state.previous].includes(manifest.revision)) throw new Error('Cannot overwrite a retained application version');
      await removeSourceDirectory(target); await fs.rename(stage, target);
      this.state.pending = manifest.revision;
      try { await this.save(); } catch (error) { delete this.state.pending; throw error; }
    } finally { await removeSourceDirectory(stage); }
  }
  async application(revision: string): Promise<SourceApplication> {
    const dir = this.directory(revision);
    const manifest = verifySourceManifest(await fs.readFile(path.join(dir, 'release.json'), 'utf8'), this.options.publicKey);
    const built = JSON.parse(await fs.readFile(path.join(dir, 'build.json'), 'utf8')) as InstalledBuild;
    if (manifest.revision !== revision || built.revision !== revision || !DIGEST.test(built.runtimeKey) || (built.pythonKey !== null && !DIGEST.test(built.pythonKey))) throw new Error('Invalid built application');
    const resources = path.join(this.options.root, 'runtimes', built.runtimeKey);
    const python = built.pythonKey ? path.join(this.options.root, 'dependencies', built.runtimeKey, 'python', built.pythonKey.slice(0, 24), 'Scripts/python.exe') : path.join(resources, 'worker/python/python.exe');
    const desktopDir = path.join(dir, 'source/desktop');
    const electron = path.resolve(built.electron);
    const dependencyRoot = path.resolve(this.options.root, 'dependencies') + path.sep;
    if (!electron.toLowerCase().startsWith(dependencyRoot.toLowerCase())) throw new Error('Desktop runtime is outside the managed dependency cache');
    for (const file of [path.join(desktopDir, 'dist/main/managed-entry.js'), path.join(dir, 'server/app/main.py'), path.join(dir, 'server/app/static/index.html'), python, electron]) {
      if (!(await fs.stat(file)).isFile()) throw new Error('Application build is incomplete');
    }
    return { revision, directory: dir, desktopDir, serverDir: path.join(dir, 'server'), resources, python, node: path.join(resources, 'worker/node/node.exe'), electron };
  }
  async current(): Promise<SourceApplication | null> { return this.state.active ? this.application(this.state.active) : null; }
  async select(): Promise<SourceApplication> {
    const revision = this.state.pending ?? this.state.active;
    if (!revision || (this.state.blocked && !this.state.pending)) throw new Error('No healthy source build is available. Check for updates. Previous files and data have been retained.');
    const application = await this.application(revision);
    if (this.state.pending) { this.state.booting = revision; await this.save(); }
    this.running = revision; return application;
  }
  /** Attach from the newly launched Electron process without interpreting its own first boot as a crash. */
  async attach(revision: string): Promise<void> {
    this.state = JSON.parse(await fs.readFile(path.join(this.options.root, 'state.json'), 'utf8'));
    if (![this.state.active, this.state.booting].includes(revision)) throw new Error('Unexpected desktop launch revision');
    await this.application(revision); this.running = revision;
  }
  async healthy(): Promise<void> {
    if (!this.state.booting || this.state.booting !== this.running) return;
    this.state.previous = this.state.active; this.state.active = this.running;
    delete this.state.pending; delete this.state.booting; delete this.state.blocked; await this.save();
    const node = (await this.application(this.running)).node;
    await fs.writeFile(path.join(this.options.root, 'node-path.txt.tmp'), node);
    await fs.rename(path.join(this.options.root, 'node-path.txt.tmp'), path.join(this.options.root, 'node-path.txt'));
    // Keep all previous native runtimes: PostgreSQL data may still need an old
    // major version. Only prune application files after a healthy new startup.
    const keep = new Set([this.state.active, this.state.previous, ...(this.state.failed ?? [])]);
    for (const entry of await fs.readdir(path.join(this.options.root, 'versions'), { withFileTypes: true })) {
      if (entry.isDirectory() && !entry.isSymbolicLink() && REVISION.test(entry.name) && !keep.has(entry.name)) await removeSourceDirectory(this.directory(entry.name));
    }
  }
  async failed(): Promise<void> {
    if (!this.state.booting) return;
    this.state.failed = [...new Set([...(this.state.failed ?? []), this.state.booting])];
    delete this.state.booting; delete this.state.pending; this.state.blocked = true; await this.save();
  }
}
