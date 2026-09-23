/**
 * New-release check for the macOS and Linux apps (decision D22: it runs on
 * launch and once a day without asking, like other desktop apps).
 *
 * Windows keeps its own path (signed source updates, desktop/src/main/source-updates.ts).
 * On macOS and Linux the app reads the hub's per-platform installer list
 * (GET /v1/desktop/installers, android/hub/src/desktop-updates.ts), verifies
 * this platform's entry with the application update key the app ships
 * (build/application-update-key.pem), and when the signed version is newer
 * than the running one, offers the installer. A download is checked against
 * the signed size and SHA-256 before it is kept.
 *
 * Only the signed payload is trusted. The entry's url, version, size and
 * sha256 fields beside it are ignored; the bytes come from the hub's fixed
 * /v1/desktop/installer/<platform> address.
 */
import { createHash, verify } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { SOURCE_ORIGIN } from './source-auth';

export const INSTALLERS_URL = SOURCE_ORIGIN + '/v1/desktop/installers';
export type InstallerPlatform = 'mac' | 'linux';
export interface InstallerPayload {
  type: 'desktop-installer'; format: 1; platform: InstallerPlatform; arch: string;
  version: string; revision: string; file: string; size: number; sha256: string;
}
export type InstallerCheck = { kind: 'current' } | { kind: 'available'; version: string; release: string; installer: InstallerPayload };
export interface InstallerOptions {
  platform: InstallerPlatform;
  /** process.arch; an entry built for another architecture is never offered. */
  arch: string;
  /** The running app's version (app.getVersion()). */
  currentVersion: string;
  publicKey: string;
  /** Where a verified installer is saved (the user's Downloads folder). */
  directory: string;
  fetch?: typeof fetch;
}

const MAX_LIST = 64 * 1024;
const MAX_INSTALLER = 2 * 1024 ** 3;
const RELEASE = /^\d{1,6}\.\d{1,6}\.\d{1,6}$/;

/** The installer platform for this OS, or null where installer checks do not apply (Windows). */
export function installerPlatform(platform: string = process.platform): InstallerPlatform | null {
  return platform === 'darwin' ? 'mac' : platform === 'linux' ? 'linux' : null;
}

/** a > b for X.Y.Z release versions; anything after the patch number (a prerelease or build tag) is ignored. */
export function isNewerVersion(a: string, b: string): boolean {
  const parse = (value: string) => value.split(/[-+]/)[0].split('.').map(Number);
  const x = parse(a), y = parse(b);
  for (let i = 0; i < 3; i++) {
    if ((x[i] ?? 0) !== (y[i] ?? 0)) return (x[i] ?? 0) > (y[i] ?? 0);
  }
  return false;
}

/** The signed payload of one platform entry, or an error: unsigned, forged, or not for this platform. */
export function verifyInstallerEntry(entry: unknown, publicKey: string, platform: InstallerPlatform): InstallerPayload {
  const e = entry as { payload?: unknown; signature?: unknown } | null;
  if (!e || typeof e.payload !== 'string' || typeof e.signature !== 'string') throw new Error('The installer entry is not signed');
  const payload = Buffer.from(e.payload, 'base64');
  if (!verify(null, payload, publicKey, Buffer.from(e.signature, 'base64'))) throw new Error('Invalid installer signature');
  const p = JSON.parse(payload.toString('utf8')) as InstallerPayload;
  if (p.type !== 'desktop-installer' || p.format !== 1 || p.platform !== platform || typeof p.arch !== 'string'
    || typeof p.version !== 'string' || !RELEASE.test(p.version) || typeof p.revision !== 'string' || !/^[a-f0-9]{40}$/.test(p.revision)
    || typeof p.file !== 'string' || !/^[A-Za-z0-9._-]{1,200}$/.test(p.file) || p.file.startsWith('.')
    || !Number.isSafeInteger(p.size) || p.size <= 0 || p.size > MAX_INSTALLER
    || typeof p.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(p.sha256)) throw new Error('Invalid installer entry');
  return p;
}

export class InstallerUpdates {
  private readonly request: typeof fetch;
  private offered: InstallerPayload | null = null;
  constructor(readonly options: InstallerOptions) {
    this.request = options.fetch ?? ((url, init) => fetch(url, { ...init, redirect: 'error', credentials: 'omit' }));
  }

  private async get(url: string, limit: number): Promise<Response> {
    const response = await this.request(url, { method: 'GET', signal: AbortSignal.timeout(120_000), headers: { 'User-Agent': 'AI-Shuffle-Installer-Check' } });
    if (!response.ok || !response.body) throw new Error(`Installer request failed (${response.status})`);
    const length = Number(response.headers.get('content-length'));
    if (Number.isFinite(length) && length > limit) throw new Error('Installer response exceeds limits');
    return response;
  }

  /** Reads the installer list and says whether a newer signed release exists for this platform. */
  async check(): Promise<InstallerCheck> {
    const response = await this.get(INSTALLERS_URL, MAX_LIST);
    const chunks: Uint8Array[] = []; let size = 0;
    for await (const chunk of response.body as unknown as AsyncIterable<Uint8Array>) {
      if ((size += chunk.length) > MAX_LIST) throw new Error('Installer list exceeds limits');
      chunks.push(chunk);
    }
    const list = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (list?.format !== 1 || !list.platforms || typeof list.platforms !== 'object') throw new Error('Unknown installer list format');
    const entry = list.platforms[this.options.platform];
    this.offered = null;
    if (!entry) return { kind: 'current' };
    const installer = verifyInstallerEntry(entry, this.options.publicKey, this.options.platform);
    if (installer.arch !== this.options.arch || !isNewerVersion(installer.version, this.options.currentVersion)) return { kind: 'current' };
    this.offered = installer;
    return { kind: 'available', version: installer.version, release: installer.version, installer };
  }

  /**
   * Downloads the installer the last check offered, checks it against the
   * signed size and SHA-256, and returns where it was saved. Nothing partial
   * or unverified is left under the final name.
   */
  async download(progress: (percent: number | null, message: string) => void = () => {}): Promise<{ file: string; version: string }> {
    const installer = this.offered ?? (await this.check(), this.offered as InstallerPayload | null);
    if (!installer) throw new Error('No newer installer is available');
    const response = await this.get(`${SOURCE_ORIGIN}/v1/desktop/installer/${installer.platform}`, installer.size);
    await fs.mkdir(this.options.directory, { recursive: true });
    const target = path.join(this.options.directory, installer.file);
    const partial = `${target}.${process.pid}.part`;
    const hash = createHash('sha256'); let size = 0;
    const file = await fs.open(partial, 'w', 0o644);
    try {
      try {
        for await (const chunk of response.body as unknown as AsyncIterable<Uint8Array>) {
          size += chunk.length;
          if (size > installer.size) throw new Error('Installer download exceeds the signed size');
          hash.update(chunk); await file.write(chunk);
          progress(Math.floor(size / installer.size * 100), `Downloading AI Shuffle ${installer.version}…`);
        }
      } finally { await file.close(); }
      if (size !== installer.size || hash.digest('hex') !== installer.sha256) throw new Error('Installer checksum or size did not match the signed release');
      await fs.rename(partial, target);
    } catch (error) {
      await fs.rm(partial, { force: true });
      throw error;
    }
    return { file: target, version: installer.version };
  }
}
