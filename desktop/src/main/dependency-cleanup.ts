/**
 * Delete update dependency folders that no kept application uses (#1275).
 *
 * Each source build links its npm packages into updates/dependencies and runs Python
 * from it. Versions are pruned after a healthy start, but their dependency folders
 * were not, so every runtime or package change left gigabytes behind. This runs after
 * a healthy start, in the app only (never in the installed launcher's code), and:
 *
 * - keeps everything any folder in versions/, staging/, state.json, the restore
 *   journal or the running build points at, read from build.json records AND the
 *   node_modules links themselves. Records only ever add protection;
 * - deletes nothing at all when any of those cannot be read, or a link points
 *   somewhere unexpected;
 * - never follows a junction or symlink: links are read with readlink, never
 *   walked, and a link is removed as a link;
 * - holds update.lock, so no update build runs at the same time, and skips when
 *   it is taken;
 * - never throws: a failure is logged and the next start tries again;
 * - leaves runtimes/ (old PostgreSQL majors are kept on purpose), builds/ and
 *   anything it does not recognise alone.
 */
import { promises as fs } from 'node:fs';
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
  try { const handle = await fs.open(lock, 'wx'); await handle.writeFile(String(process.pid)); await handle.close(); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'EEXIST') return { ...result, skipped: 'an update is running' };
    if (missing(error)) return { ...result, skipped: 'there is no update folder' };
    return skip(result, error, logger);
  }
  try {
    const keep = await protectedPaths(root, state, running);
    for (const candidate of await candidates(path.join(root, 'dependencies'), keep)) {
      try {
        await removeDependencyDirectory(path.join(root, 'dependencies', ...candidate));
        // A rejected npm set's tombstone sits beside it; it means nothing once the set is gone.
        if (candidate.at(-3) === 'node') await fs.rm(path.join(root, 'dependencies', ...candidate.slice(0, -1), '.invalid-' + candidate.at(-1)), { force: true });
        result.removed.push(candidate.join('/'));
      } catch (error) {
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

/** Dependency-relative paths something still uses. A folder is kept when one of them is it or lies inside it. */
async function protectedPaths(root: string, state: SourceState, running: string | null): Promise<Set<string>> {
  const versions = path.join(root, 'versions'), dependencies = path.join(root, 'dependencies');
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
  const folders = new Set<string>([...revisions].map(revision => path.join(versions, revision)));
  let entries: import('node:fs').Dirent[] = [];
  try { entries = await fs.readdir(versions, { withFileTypes: true }); }
  catch (error) { if (!missing(error)) throw new Unsafe('the installed versions cannot be listed'); }
  for (const entry of entries) {
    if (entry.isSymbolicLink()) throw new Unsafe(`versions/${entry.name} is a link`);
    if (entry.isDirectory()) folders.add(path.join(versions, entry.name));
  }
  folders.add(path.join(root, 'staging'));
  const roots = [path.resolve(dependencies)];
  try { const real = await fs.realpath(dependencies); if (!roots.includes(real)) roots.push(real); } catch { /* no dependencies yet */ }
  const keep = new Set<string>();
  const protect = (target: string, origin: string) => {
    const absolute = path.resolve(target.replace(/^\\\\\?\\UNC\\/, '\\\\').replace(/^\\\\\?\\/, ''));
    for (const base of roots) {
      const relative = path.relative(base, absolute);
      if (relative && !relative.startsWith('..') && !path.isAbsolute(relative)) { keep.add(key(relative.split(path.sep))); return; }
      if (!relative) throw new Unsafe(`${origin} points at the whole dependency folder`);
    }
    // mobile/node_modules links to the same version's frontend/node_modules.
    const version = path.relative(path.resolve(versions), absolute), staged = path.relative(path.resolve(root, 'staging'), absolute);
    for (const inside of [version, staged]) if (inside && !inside.startsWith('..') && !path.isAbsolute(inside)) return;
    throw new Unsafe(`${origin} points outside the update folders`);
  };
  for (const folder of folders) {
    const name = path.basename(folder);
    if (REVISION.test(name)) {
      let built: Record<string, unknown> | null = null;
      try { built = JSON.parse(await fs.readFile(path.join(folder, 'build.json'), 'utf8')); }
      catch (error) { if (!missing(error)) throw new Unsafe(`versions/${name}/build.json cannot be read`); }
      if (built) {
        const { runtimeKey, pythonKey, layout, nodeGenerations, electron } = built as { runtimeKey: unknown; pythonKey: unknown; layout: unknown; nodeGenerations: unknown; electron: unknown };
        if (typeof runtimeKey !== 'string' || !DIGEST.test(runtimeKey) || (pythonKey !== null && (typeof pythonKey !== 'string' || !DIGEST.test(pythonKey))) ||
            (layout !== undefined && layout !== 2) || (nodeGenerations !== undefined && (!Array.isArray(nodeGenerations) || nodeGenerations.some(id => typeof id !== 'string' || !NODE_GENERATION.test(id)))) ||
            typeof electron !== 'string') throw new Unsafe(`versions/${name}/build.json is not a build record`);
        const tree = layout === 2 ? runtimeKey.slice(0, 24) : runtimeKey;
        keep.add(key([tree]));
        if (pythonKey) keep.add(key([tree, 'python', pythonKey.slice(0, 24)]));
        for (const id of (nodeGenerations as string[] | undefined) ?? []) keep.add(key(id.split('/')));
        protect(electron, `versions/${name}/build.json`);
      }
    }
    for (const base of ['source', 'server']) for (const pkg of LINKED_PACKAGES) {
      const link = path.join(folder, base, pkg, 'node_modules');
      let stat;
      try { stat = await fs.lstat(link); } catch (error) { if (missing(error)) continue; throw new Unsafe(`${link} cannot be read`); }
      if (!stat.isSymbolicLink()) continue; // A real folder holds its own packages.
      let target: string;
      try { target = await fs.readlink(link); } catch { throw new Unsafe(`${link} cannot be read`); }
      protect(path.resolve(path.dirname(link), target), link);
    }
  }
  return keep;
}

/** Unused dependency folders as path segments under updates/dependencies. Links and unknown names are never listed. */
async function candidates(dependencies: string, keep: Set<string>): Promise<string[][]> {
  const kept = (segments: string[]) => { const self = key(segments); for (const used of keep) if (used === self || used.startsWith(self + '/')) return true; return false; };
  const directories = async (segments: string[], pattern: RegExp) => {
    try { return (await fs.readdir(path.join(dependencies, ...segments), { withFileTypes: true })).filter(entry => entry.isDirectory() && !entry.isSymbolicLink() && pattern.test(entry.name)).map(entry => [...segments, entry.name]); }
    catch (error) { if (missing(error)) return []; throw error; }
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
