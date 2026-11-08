/**
 * Delete update dependency folders that no kept application uses (#1275).
 *
 * Each source build links its npm packages into updates/dependencies and runs Python
 * from it. Versions are pruned after a healthy start, but their dependency folders
 * were not, so every runtime or package change left gigabytes behind. This runs after
 * a healthy start, in the app only (never in the installed launcher's code), and:
 *
 * - keeps everything any folder in versions/ (whatever its name), staging/,
 *   state.json, the restore journal or the running build points at, read from
 *   build.json records AND the node_modules links themselves, by the path they use
 *   and by where that path really is. Records only ever add protection;
 * - deletes nothing at all when any of those cannot be read, the active or running
 *   build has no readable record, or a link points somewhere unexpected;
 * - never follows a junction or symlink: links are read with readlink, never
 *   walked, and a link is removed as a link. A link anywhere on the way to a
 *   folder it would list or delete, including updates/dependencies itself, stops
 *   the whole cleanup;
 * - holds update.lock, so no update build runs at the same time, and skips when
 *   it is taken;
 * - never throws: a failure is logged and the next start tries again;
 * - leaves runtimes/ (old PostgreSQL majors are kept on purpose), builds/ and
 *   anything it does not recognise alone.
 */
import { promises as fs } from 'node:fs';
import type { FileHandle } from 'node:fs/promises';
import path from 'node:path';
import { parseSourceState, type SourceState } from './source-updates';

const REVISION = /^[a-f0-9]{40}$/;
const DIGEST = /^[a-f0-9]{64}$/;
/** Per-runtime trees: 24 hex since layout 2, the full runtime key before. */
const RUNTIME_TREE = /^(?:[a-f0-9]{24}|[a-f0-9]{64})$/;
const NODE_PACKAGES = ['frontend', 'tools', 'browser-mcp', 'desktop'];
/** A completed npm set, or a partial install left by an interrupted one. */
const NODE_SET = /^[a-f0-9]{24}(?:-[a-z0-9]+-[a-f0-9]{8})?(?:\.tmp-[a-f0-9]{8})?$/;
/** A Python environment, or a partial one left by an interrupted install. */
const PYTHON_SET = /^[a-f0-9]{24}(?:\.tmp-[a-f0-9]{8})?$/;
const NODE_GENERATION = /^(?:[a-f0-9]{24}(?:[a-f0-9]{40})?\/)?node\/(?:frontend|tools|browser-mcp|desktop)\/[a-f0-9]{24}(?:-[a-z0-9]+-[a-f0-9]{8})?$/;
/** Every node_modules link a build makes (removeSourceDirectory detaches the same ones). */
const LINKED_PACKAGES = ['frontend', 'mobile', 'tools', 'browser-mcp', 'desktop'];

export interface DependencyCleanup { removed: string[]; failed: string[]; skipped?: string }
interface Logger { log(...args: unknown[]): void; warn(...args: unknown[]): void }

class Unsafe extends Error {}

const windows = process.platform === 'win32';
const key = (segments: string[]) => { const joined = segments.join('/'); return windows ? joined.toLowerCase() : joined; };
const samePath = (a: string, b: string) => windows ? a.toLowerCase() === b.toLowerCase() : a === b;
const missing = (error: unknown) => (error as NodeJS.ErrnoException).code === 'ENOENT';

/** Remove one dependency folder. A link is unlinked, never entered. */
export async function removeDependencyDirectory(directory: string): Promise<void> {
  const stat = await fs.lstat(directory);
  if (stat.isSymbolicLink()) { await fs.unlink(directory); return; }
  await fs.rm(directory, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
}

export async function pruneDependencies(root: string, running: string | null, logger: Logger = console): Promise<DependencyCleanup> {
  const result: DependencyCleanup = { removed: [], failed: [] };
  let state: SourceState;
  try {
    try { state = parseSourceState(await fs.readFile(path.join(root, 'state.json'), 'utf8')); }
    catch (error) { if (!missing(error)) throw new Unsafe('the update state cannot be read'); state = {}; }
    if (state.booting || state.waiting) return { ...result, skipped: 'a first start has not finished yet' };
  } catch (error) { return skip(result, error, logger); }
  const lock = path.join(root, 'update.lock');
  let handle: FileHandle;
  try { handle = await fs.open(lock, 'wx'); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'EEXIST') return { ...result, skipped: 'an update is running' };
    if (missing(error)) return { ...result, skipped: 'there is no update folder' };
    return skip(result, error, logger);
  }
  // From here the lock file is ours, so it is always closed and removed again, even
  // when writing it failed. Nothing here ever removes a lock another process made.
  try {
    try { await handle.writeFile(String(process.pid)); } finally { await handle.close(); }
    const dependencies = path.join(root, 'dependencies');
    const real = await plainDependencies(dependencies);
    if (real === null) return result;
    const keep = await protectedPaths(root, state, running, real);
    for (const candidate of await candidates(dependencies, real, keep)) {
      try {
        // Checked again just before deleting: no folder on the way may have become a link.
        if (!await assertPlain(dependencies, real, candidate.slice(0, -1))) continue;
        await removeDependencyDirectory(path.join(dependencies, ...candidate));
        // A rejected npm set's tombstone sits beside it; it means nothing once the set is gone.
        if (candidate.at(-3) === 'node') await fs.rm(path.join(dependencies, ...candidate.slice(0, -1), '.invalid-' + candidate.at(-1)), { force: true });
        result.removed.push(candidate.join('/'));
      } catch (error) {
        if (error instanceof Unsafe) throw error;
        result.failed.push(candidate.join('/'));
        logger.warn(`Could not remove the unused update dependency folder ${candidate.join('/')}; the next start tries again`, error);
      }
    }
    if (result.removed.length) logger.log(`Removed ${result.removed.length} unused update dependency folder(s): ${result.removed.join(', ')}`);
    return result;
  } catch (error) {
    return skip(result, error, logger);
  } finally {
    await fs.unlink(lock).catch(() => {});
  }
}

function skip(result: DependencyCleanup, error: unknown, logger: Logger): DependencyCleanup {
  const reason = error instanceof Unsafe ? error.message : String((error as Error)?.message ?? error);
  logger.warn(`Kept every update dependency folder: ${reason}`);
  return { ...result, skipped: reason };
}

/**
 * The real location of updates/dependencies, or null when there is none yet.
 * The folder itself must not be a link: everything below is matched by name, so
 * an alias would let a kept folder be deleted under another name.
 */
async function plainDependencies(dependencies: string): Promise<string | null> {
  let stat;
  try { stat = await fs.lstat(dependencies); } catch (error) { if (missing(error)) return null; throw new Unsafe('the dependency folder cannot be read'); }
  if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Unsafe('the dependency folder is a link or not a folder');
  return fs.realpath(dependencies);
}

/**
 * Every folder from updates/dependencies down to `segments` must be a real folder at
 * exactly that place: no junction or symlink on the way, checked by name and by real
 * location. Returns false when one of them does not exist.
 */
async function assertPlain(dependencies: string, real: string, segments: string[]): Promise<boolean> {
  for (let depth = 1; depth <= segments.length; depth++) {
    const name = `dependencies/${segments.slice(0, depth).join('/')}`, at = path.join(dependencies, ...segments.slice(0, depth));
    let stat;
    try { stat = await fs.lstat(at); } catch (error) { if (missing(error)) return false; throw new Unsafe(`${name} cannot be read`); }
    if (stat.isSymbolicLink()) throw new Unsafe(`${name} is a link`);
    if (!stat.isDirectory()) return false;
    if (!samePath(await fs.realpath(at), path.join(real, ...segments.slice(0, depth)))) throw new Unsafe(`${name} is somewhere else on disk`);
  }
  return true;
}

/** Dependency-relative paths something still uses. A folder is kept when one of them is it or lies inside it. */
async function protectedPaths(root: string, state: SourceState, running: string | null, real: string): Promise<Set<string>> {
  const versions = path.join(root, 'versions'), staging = path.join(root, 'staging'), dependencies = path.join(root, 'dependencies');
  const required = new Set([state.active, running].filter((value): value is string => !!value));
  const revisions = new Set<string>([state.active, state.previous, state.pending, state.staged, state.booting, state.waiting,
    ...(state.failed ?? []), ...(state.supersededFailed ?? []), ...Object.keys(state.mayHaveMigrated ?? {}), running ?? undefined]
    .filter((value): value is string => !!value));
  try {
    const journal = JSON.parse(await fs.readFile(path.join(root, 'restore-point.json'), 'utf8'));
    for (const revision of [journal?.revision, journal?.previous, journal?.state?.active, journal?.state?.previous]) {
      if (revision !== undefined && (typeof revision !== 'string' || !REVISION.test(revision))) throw new Unsafe('the database restore journal is invalid');
      if (revision) revisions.add(revision);
    }
  } catch (error) { if (error instanceof Unsafe) throw error; if (!missing(error)) throw new Unsafe('the database restore journal cannot be read'); }
  for (const revision of revisions) if (!REVISION.test(revision)) throw new Unsafe(`an unexpected revision ${revision} is recorded`);
  for (const container of [versions, staging]) {
    let stat;
    try { stat = await fs.lstat(container); } catch (error) { if (missing(error)) continue; throw new Unsafe(`${path.basename(container)}/ cannot be read`); }
    if (stat.isSymbolicLink()) throw new Unsafe(`${path.basename(container)}/ is a link`);
  }
  // Every folder in versions/ is read, whatever its name: a kept build can sit under
  // a temporary name (for example <revision>.restoring) while it is being put back.
  let entries: import('node:fs').Dirent[] = [];
  try { entries = await fs.readdir(versions, { withFileTypes: true }); }
  catch (error) { if (!missing(error)) throw new Unsafe('the installed versions cannot be listed'); }
  const folders = [staging];
  for (const entry of entries) {
    if (entry.isSymbolicLink()) throw new Unsafe(`versions/${entry.name} is a link`);
    if (entry.isDirectory()) folders.push(path.join(versions, entry.name));
  }
  const roots = [path.resolve(dependencies)];
  if (!roots.some(base => samePath(base, real))) roots.push(real);
  /** The path of `absolute` below `base`, '' for base itself, null when outside. */
  const below = (base: string, absolute: string) => {
    const relative = path.relative(base, absolute);
    return relative === '' ? '' : !relative.startsWith('..') && !path.isAbsolute(relative) ? relative : null;
  };
  const keep = new Set<string>();
  /** Protect what a link or record points at, by the path it uses and by where that path really is. */
  const protect = async (target: string, origin: string) => {
    const lexical = path.resolve(target.replace(/^\\\\\?\\UNC\\/, '\\\\').replace(/^\\\\\?\\/, ''));
    let physical: string | null = null;
    try { physical = await fs.realpath(lexical); } catch (error) { if (!missing(error)) throw new Unsafe(`${origin} cannot be resolved`); }
    let found = false;
    for (const absolute of [lexical, physical]) {
      if (absolute === null) continue;
      for (const base of roots) {
        const relative = below(base, absolute);
        if (relative === '') throw new Unsafe(`${origin} points at the whole dependency folder`);
        if (relative) { keep.add(key(relative.split(path.sep))); found = true; }
      }
    }
    if (found) return;
    // mobile/node_modules links to the same version's frontend/node_modules.
    if (below(path.resolve(versions), lexical) || below(path.resolve(staging), lexical)) return;
    throw new Unsafe(`${origin} points outside the update folders`);
  };
  const recorded = new Set<string>();
  for (const folder of folders) {
    const name = path.basename(folder), origin = folder === staging ? 'staging/build.json' : `versions/${name}/build.json`;
    let built: Record<string, unknown> | null = null;
    try { built = JSON.parse(await fs.readFile(path.join(folder, 'build.json'), 'utf8')); }
    catch (error) { if (!missing(error)) throw new Unsafe(`${origin} cannot be read`); }
    if (built) {
      const { revision, runtimeKey, pythonKey, layout, nodeGenerations, electron } = built as { revision: unknown; runtimeKey: unknown; pythonKey: unknown; layout: unknown; nodeGenerations: unknown; electron: unknown };
      if ((revision !== undefined && (typeof revision !== 'string' || !REVISION.test(revision))) ||
          typeof runtimeKey !== 'string' || !DIGEST.test(runtimeKey) || (pythonKey !== null && (typeof pythonKey !== 'string' || !DIGEST.test(pythonKey))) ||
          (layout !== undefined && layout !== 2) || (nodeGenerations !== undefined && (!Array.isArray(nodeGenerations) || nodeGenerations.some(id => typeof id !== 'string' || !NODE_GENERATION.test(id)))) ||
          typeof electron !== 'string') throw new Unsafe(`${origin} is not a build record`);
      const tree = layout === 2 ? runtimeKey.slice(0, 24) : runtimeKey;
      keep.add(key([tree]));
      if (pythonKey) keep.add(key([tree, 'python', pythonKey.slice(0, 24)]));
      for (const id of (nodeGenerations as string[] | undefined) ?? []) keep.add(key(id.split('/')));
      await protect(electron, origin);
      if (folder !== staging) recorded.add(typeof revision === 'string' ? revision : name.slice(0, 40));
    }
    for (const base of ['source', 'server']) for (const pkg of LINKED_PACKAGES) {
      const link = path.join(folder, base, pkg, 'node_modules');
      let stat;
      try { stat = await fs.lstat(link); } catch (error) { if (missing(error)) continue; throw new Unsafe(`${link} cannot be read`); }
      if (!stat.isSymbolicLink()) continue; // A real folder holds its own packages.
      let target: string;
      try { target = await fs.readlink(link); } catch { throw new Unsafe(`${link} cannot be read`); }
      await protect(path.resolve(path.dirname(link), target), link);
    }
  }
  // A kept revision with files in versions/ (under any name) must have a readable build
  // record, or what it runs from is unknown. The active and running builds always need
  // one; only an older revision may have no files here (pruned, or the installer's copy).
  for (const revision of revisions) {
    if (recorded.has(revision)) continue;
    if (required.has(revision)) throw new Unsafe(`the build record of the ${revision === running ? 'running' : 'active'} version ${revision} cannot be found`);
    if (entries.some(entry => entry.name.startsWith(revision))) throw new Unsafe(`the build record of the kept version ${revision} cannot be found`);
  }
  return keep;
}

/** Unused dependency folders as path segments under updates/dependencies. Links and unknown names are never listed. */
async function candidates(dependencies: string, real: string, keep: Set<string>): Promise<string[][]> {
  const kept = (segments: string[]) => { const self = key(segments); for (const used of keep) if (used === self || used.startsWith(self + '/')) return true; return false; };
  const directories = async (segments: string[], pattern: RegExp) => {
    // Never list through a link: a folder reached that way is not where its name says.
    if (!await assertPlain(dependencies, real, segments)) return [];
    return (await fs.readdir(path.join(dependencies, ...segments), { withFileTypes: true }))
      .filter(entry => entry.isDirectory() && !entry.isSymbolicLink() && pattern.test(entry.name)).map(entry => [...segments, entry.name]);
  };
  const unused: string[][] = [];
  const nodeSets = async (base: string[]) => { for (const pkg of NODE_PACKAGES) for (const set of await directories([...base, 'node', pkg], NODE_SET)) if (!kept(set)) unused.push(set); };
  await nodeSets([]);
  for (const tree of await directories([], RUNTIME_TREE)) {
    if (!kept(tree)) { unused.push(tree); continue; }
    for (const set of await directories([...tree, 'python'], PYTHON_SET)) if (!kept(set)) unused.push(set);
    await nodeSets(tree);
  }
  return unused;
}
