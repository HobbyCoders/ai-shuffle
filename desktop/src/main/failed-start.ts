/**
 * An update whose first start did not finish, read from the state its launcher
 * left behind (desktop/LAUNCHER.md, "A first start that did not finish").
 *
 * The launcher marks a pending build failed when its process never wrote
 * `waiting`, even when that process was only slow to load (a freshly built
 * Electron on a disk an antivirus is scanning). When the launcher also put the
 * database back from the update's restore point, nothing is left that the
 * update could have changed: `recoverRollback` leaves the revision in both
 * `failed` and `supersededFailed`, no fence and no block. Starting it again is
 * then as safe as the first attempt, with a new restore point, so the updated
 * app offers that retry instead of refusing the revision for good.
 *
 * This file is not in the launcher closure (scripts/ci/update_reach.py): it
 * only runs in the updated app, so installed launchers need no change.
 */
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { durableJson } from './database-restore';
import { INSTALLERS_URL, isNewerVersion } from './installer-updates';
import { parseSourceState, type SourceState } from './source-updates';

const REVISION = /^[a-f0-9]{40}$/;
/**
 * How many times one revision is offered again. A build that fails every
 * start then stays refused until a newer update, instead of being offered
 * (and rolled back) forever.
 */
export const MAX_START_RETRIES = 3;
/** {"<revision>": times offered again}, in the update root. Only this file writes it. */
export const RETRIES_FILE = 'start-retries.json';

async function readRetries(root: string): Promise<Record<string, number>> {
  try {
    const value = JSON.parse(await fs.readFile(path.join(root, RETRIES_FILE), 'utf8'));
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter((entry): entry is [string, number] =>
      REVISION.test(entry[0]) && Number.isSafeInteger(entry[1]) && (entry[1] as number) > 0));
  } catch { return {}; }
}

/** state.json, only as the current updater writes it (its exact serialization). A legacy writer's state is left to the launcher. */
async function readState(root: string): Promise<SourceState | null> {
  let text: string;
  try { text = await fs.readFile(path.join(root, 'state.json'), 'utf8'); }
  catch { return null; }
  try {
    const state = parseSourceState(text);
    return state.schemaVersion === 1 && text === JSON.stringify(state, null, 2) + '\n' ? state : null;
  } catch { return null; }
}

async function exists(file: string): Promise<boolean> {
  try { await fs.lstat(file); return true; }
  catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return false; throw error; }
}

/** The database floor's revision; undefined when there is none, null when it is unreadable or uncertain. */
async function floorRevision(root: string): Promise<string | null | undefined> {
  let text: string;
  try { text = await fs.readFile(path.join(root, 'database-floor.json'), 'utf8'); }
  catch (error) { return (error as NodeJS.ErrnoException).code === 'ENOENT' ? undefined : null; }
  try {
    const floor = JSON.parse(text);
    return floor?.schemaVersion === 1 && typeof floor.revision === 'string' && REVISION.test(floor.revision) && floor.uncertain === undefined ? floor.revision : null;
  } catch { return null; }
}

async function retryable(root: string): Promise<{ state: SourceState; revisions: string[]; retries: Record<string, number> } | null> {
  const state = await readState(root);
  // Anything in flight, fenced or blocked is the launcher's to settle, never this.
  if (!state || state.booting || state.pending || state.waiting || state.blocked || state.recoveryRequired ||
      state.recoveredState || Object.keys(state.mayHaveMigrated ?? {}).length) return null;
  // An unfinished restore point: the database may not be back yet.
  if (await exists(path.join(root, 'restore-point.json'))) return null;
  const floor = await floorRevision(root);
  if (floor === null) return null;
  // The floor names the code that last opened the database; a revision that holds it was not put back.
  const retries = await readRetries(root);
  const revisions = (state.failed ?? []).filter(revision => state.supersededFailed?.includes(revision) && revision !== floor &&
    (retries[revision] ?? 0) < MAX_START_RETRIES);
  return revisions.length ? { state, revisions, retries } : null;
}

/** Failed revisions whose first start was fully put back, so the same update may start again. */
export async function retryableStarts(root: string): Promise<string[]> {
  return (await retryable(root))?.revisions ?? [];
}

/**
 * Take these revisions out of `failed`, so the next check offers them like any
 * other update; the owner still chooses to download and install. Returns the
 * revisions released. The caller must reload its SourceUpdates (load())
 * afterwards. The count is written first: a crash in between can only use up
 * a retry, never grant an extra one.
 */
export async function releaseStarts(root: string, revisions: string[]): Promise<string[]> {
  const found = await retryable(root);
  const released = found ? found.revisions.filter(revision => revisions.includes(revision)) : [];
  if (!found || !released.length) return [];
  const retries = { ...found.retries };
  for (const revision of released) retries[revision] = (retries[revision] ?? 0) + 1;
  await durableJson(path.join(root, RETRIES_FILE), retries);
  const failed = (found.state.failed ?? []).filter(revision => !released.includes(revision));
  if (failed.length) found.state.failed = failed; else delete found.state.failed;
  await durableJson(path.join(root, 'state.json'), found.state);
  return released;
}

/** True while `revision` is an update on its first start (the launcher's `booting`). */
export async function isFirstStart(root: string, revision: string): Promise<boolean> {
  return (await readState(root))?.booting === revision;
}

const PLATFORMS: Partial<Record<NodeJS.Platform, string>> = { win32: 'win', darwin: 'mac', linux: 'linux' };

/**
 * Whether the hub's latest installer for this platform is a newer release than
 * `running`; null when that cannot be told. Only decides whether to point the
 * owner at the installer page, so the entry's unsigned version is enough: the
 * Windows entry carries no signed payload, and nothing is downloaded here.
 */
export async function installerNewer(running: string, platform: NodeJS.Platform = process.platform, request: typeof fetch = fetch): Promise<boolean | null> {
  const key = PLATFORMS[platform];
  if (!key) return null;
  try {
    const response = await request(INSTALLERS_URL, { method: 'GET', redirect: 'error', credentials: 'omit',
      signal: AbortSignal.timeout(15_000), headers: { 'User-Agent': 'AI-Shuffle-Installer-Check' } });
    if (!response.ok) return null;
    const text = await response.text();
    if (text.length > 64 * 1024) return null;
    const version = JSON.parse(text)?.platforms?.[key]?.version;
    return typeof version === 'string' && /^\d{1,6}\.\d{1,6}\.\d{1,6}$/.test(version) ? isNewerVersion(version, running) : null;
  } catch { return null; }
}
