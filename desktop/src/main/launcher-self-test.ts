/**
 * The launcher self-test (#1821 D5 step 3), run from inside a candidate
 * app.new.asar by the installed EXE in node mode:
 *
 *   ELECTRON_RUN_AS_NODE=1 "AI Shuffle.exe" <resources>/app.new.asar/dist/main/launcher-self-test.js
 *     --nonce=<hex> --expect=<launcher version> --root=<profile>/updates
 *
 * Read-only. It proves the archive opens, its launcher code loads, and it can
 * read and verify this install's state the way the launcher will. One JSON
 * line with the caller's nonce is the verdict; the caller ignores anything else.
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

function argument(name: string): string {
  const prefix = `--${name}=`;
  const value = process.argv.slice(2).find(arg => arg.startsWith(prefix));
  if (value === undefined) throw new Error(`Missing ${prefix}`);
  return value.slice(prefix.length);
}

function run(nonce: string): number {
  const expect = Number(argument('expect')); const root = argument('root');
  if (!Number.isSafeInteger(expect) || expect < 1 || !path.isAbsolute(root)) throw new Error('Invalid self-test arguments');
  const archive = path.resolve(__dirname, '../..');
  if (!path.basename(archive).toLowerCase().endsWith('.asar')) throw new Error('The self-test is not running from an archive');
  const { parseLauncher } = require('./launcher-swap') as typeof import('./launcher-swap');
  if (parseLauncher(fs.readFileSync(path.join(archive, 'launcher.json'), 'utf8')).version !== expect) throw new Error('The archive holds a different launcher version');
  const main = JSON.parse(fs.readFileSync(path.join(archive, 'package.json'), 'utf8')).main;
  if (main !== 'dist/main/managed-entry.js') throw new Error('The archive has a different entry point');
  // Every launcher module that does not need Electron loads; the rest compiles.
  for (const id of ['./source-updates', './installed-host', './source-profile', './asar', 'yauzl']) require(id);
  for (const name of fs.readdirSync(__dirname)) {
    if (name.endsWith('.js')) new vm.Script(fs.readFileSync(path.join(__dirname, name), 'utf8'), { filename: name });
  }
  for (const entry of ['managed-entry.js', 'installed-entry.js']) {
    for (const [, id] of fs.readFileSync(path.join(__dirname, entry), 'utf8').matchAll(/require\("(\.[^"]+)"\)/g)) require.resolve(id);
  }
  // The launcher protocol (desktop/LAUNCHER.md) with this archive's code and key.
  const { parseSourceState, verifySourceManifest } = require('./source-updates') as typeof import('./source-updates');
  const { readInstalledHostSync } = require('./installed-host') as typeof import('./installed-host');
  if (!readInstalledHostSync(root)) throw new Error('No installed launcher record');
  const key = fs.readFileSync(path.join(archive, 'build/application-update-key.pem'), 'utf8');
  let state = {} as ReturnType<typeof parseSourceState>;
  try { state = parseSourceState(fs.readFileSync(path.join(root, 'state.json'), 'utf8')); }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
  const revision = state.pending ?? state.active;
  if (revision && verifySourceManifest(fs.readFileSync(path.join(root, 'versions', revision, 'release.json'), 'utf8'), key).revision !== revision) {
    throw new Error('The selected release does not verify with this archive');
  }
  process.stdout.write(JSON.stringify({ launcherSelfTest: 1, nonce, version: expect, ok: true }) + '\n');
  return 0;
}

let nonce = '';
try { nonce = argument('nonce'); process.exitCode = run(nonce); }
catch (error) {
  process.stdout.write(JSON.stringify({ launcherSelfTest: 1, nonce, ok: false, error: String(error).slice(0, 500) }) + '\n');
  process.exitCode = 1;
}
