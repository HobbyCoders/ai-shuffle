// #1821 D5 steps 2-4: replacing the installed Windows launcher (desktop/LAUNCHER.md).
// Linux unit tests only; the Windows acceptance run is desktop-installer.yml.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { createHash, generateKeyPairSync, sign } from 'node:crypto';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { readLauncher } from '../scripts/launcher-contract.mjs';
const require = createRequire(import.meta.url);
const { packAsar, readAsarMember } = require('../dist/main/asar.js');
const swap = require('../dist/main/launcher-swap.js');
const { SourceUpdates } = require('../dist/main/source-updates.js');
const realDesktop = fileURLToPath(new URL('..', import.meta.url));

const A = 'a'.repeat(40), B = 'b'.repeat(40);
const ENTRY = 'dist/main/managed-entry.js';
const INSTALLED_PACKAGE = JSON.stringify({ name: 'ai-shuffle-desktop', version: '0.9.1', main: ENTRY });
const sha = data => createHash('sha256').update(data).digest('hex');
const passing = async () => {};

function tmp(t) {
 const dir = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'launcher-swap-'));
 t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
 return dir;
}
function write(root, files) {
 for (const [name, value] of Object.entries(files)) { fs.mkdirSync(path.dirname(path.join(root, name)), { recursive: true }); fs.writeFileSync(path.join(root, name), value); }
}
/** A built desktop source folder with the files electron-builder.yml packs, plus some it does not. */
function fakeDesktop(dir, { version = 2, main = ENTRY } = {}) {
 const desktop = path.join(dir, 'source', 'desktop');
 write(desktop, {
  [ENTRY]: 'new entry', 'dist/main/launcher-self-test.js': 'self test', 'dist/renderer/app.js': 'renderer', 'src/pages/index.html': '<p>',
  'build/icon.png': 'png', 'build/application-update-key.pem': 'pem', 'build/not-packed.txt': 'no', 'scripts/source-profile.cjs': 'cjs', 'scripts/not-packed.mjs': 'no',
  'launcher.json': JSON.stringify({ version, minimum: 0 }), 'package.json': JSON.stringify({ name: 'ai-shuffle-desktop', version: '0.9.2', main }),
  'package-lock.json': JSON.stringify({ packages: { '': {}, 'node_modules/yauzl': {}, 'node_modules/@scope/lib': {}, 'node_modules/electron': { dev: true }, 'node_modules/maybe': { devOptional: true } } }),
  'node_modules/yauzl/index.js': 'yauzl', 'node_modules/@scope/lib/index.js': 'lib', 'node_modules/electron/index.js': 'dev', 'node_modules/maybe/index.js': 'dev',
 });
 return desktop;
}
/** An installed app (per-user, as NSIS installs it) whose launcher is version `launcher`, and its profile. */
function install(t, { launcher = 1, desktop } = {}) {
 const dir = tmp(t);
 const exe = path.join(dir, 'Programs', 'AI Shuffle', 'AI Shuffle.exe');
 const resources = path.join(path.dirname(exe), 'resources');
 fs.mkdirSync(resources, { recursive: true });
 // Relaunched by the restore helper on Linux: record that it ran.
 fs.writeFileSync(exe, '#!/bin/sh\necho started >> "$(dirname "$0")/relaunched"\n', { mode: 0o755 });
 const original = packAsar([
  { name: 'package.json', data: Buffer.from(INSTALLED_PACKAGE) },
  { name: 'launcher.json', data: Buffer.from(JSON.stringify({ version: launcher, minimum: 0 })) },
  { name: ENTRY, data: Buffer.from('old entry') },
 ]);
 fs.writeFileSync(path.join(resources, 'app.asar'), original);
 const userData = path.join(dir, 'profile');
 const root = path.join(userData, 'updates');
 fs.mkdirSync(root, { recursive: true });
 const host = { executable: exe, userData, name: 'AI Shuffle' };
 fs.writeFileSync(path.join(root, 'installed-host.json'), JSON.stringify(host));
 const desktopDir = desktop ?? fakeDesktop(dir);
 return {
  dir, exe, resources, root, host, original, desktopDir,
  application: { revision: A, desktopDir }, manifest: { revision: A, desktop: { launcher: 2 } },
  asar: () => fs.readFileSync(path.join(resources, 'app.asar')),
  has: name => fs.existsSync(path.join(resources, name)),
  marker: () => swap.readMarker(root, fs),
  options(extra = {}) { return { root, host, application: this.application, manifest: this.manifest, running: 1, selfTest: passing, fs, ...extra }; },
 };
}

test('the archive writer produces what @electron/asar (Electron\'s own format) reads, integrity included', t => {
 const dir = tmp(t);
 const asar = require('@electron/asar');
 const big = Buffer.alloc(4 * 1024 * 1024 + 7, 1); big[5] = 9;
 const members = [{ name: 'package.json', data: Buffer.from('{"main":"x.js"}') }, { name: 'dist/main/x.js', data: Buffer.from('ok') },
  { name: 'dist/empty.txt', data: Buffer.alloc(0) }, { name: 'node_modules/@s/p/big.bin', data: big }];
 const file = path.join(dir, 'ours.asar');
 fs.writeFileSync(file, packAsar(members));
 assert.deepEqual(asar.listPackage(file, {}).map(name => name.split(path.sep).join('/')).sort(),
  ['/dist', '/dist/empty.txt', '/dist/main', '/dist/main/x.js', '/node_modules', '/node_modules/@s', '/node_modules/@s/p', '/node_modules/@s/p/big.bin', '/package.json']);
 for (const { name, data } of members) {
  assert.deepEqual(asar.extractFile(file, name), data, name);
  assert.deepEqual(readAsarMember(fs, file, name), data, name);
 }
 // The integrity record Electron checks when its fuse is on: the same as @electron/asar writes for the same files.
 const plain = path.join(dir, 'plain'); write(plain, Object.fromEntries(members.map(m => [m.name, m.data])));
 const theirs = path.join(dir, 'theirs.asar');
 return asar.createPackage(plain, theirs).then(() => {
  for (const { name } of members) assert.deepEqual(asar.statFile(file, name).integrity, asar.statFile(theirs, name).integrity, name);
  // And ours reads theirs.
  assert.deepEqual(readAsarMember(fs, theirs, 'node_modules/@s/p/big.bin'), big);
 });
});

test('the archive reader refuses changed bytes, missing members and non-archives; the writer refuses unsafe names', t => {
 const dir = tmp(t);
 const file = path.join(dir, 'a.asar');
 const bytes = packAsar([{ name: 'package.json', data: Buffer.from('{"main":"x"}') }]);
 bytes[bytes.length - 2] ^= 1; fs.writeFileSync(file, bytes);
 assert.throws(() => readAsarMember(fs, file, 'package.json'), /integrity/);
 assert.throws(() => readAsarMember(fs, file, 'nope.json'), /not in/);
 fs.writeFileSync(file, 'not an archive at all');
 assert.throws(() => readAsarMember(fs, file, 'package.json'), /asar/);
 for (const name of ['', '/abs', '../up', 'a/../b', 'a//b', './a', 'a\\b']) assert.throws(() => packAsar([{ name, data: Buffer.alloc(1) }]), /Invalid archive member/, name);
 assert.throws(() => packAsar([{ name: 'a', data: Buffer.alloc(1) }, { name: 'a', data: Buffer.alloc(1) }]), /Duplicate/);
 assert.throws(() => packAsar([{ name: 'a', data: Buffer.alloc(1) }, { name: 'a/b', data: Buffer.alloc(1) }]), /inside a file/);
});

test('the launcher reads launcher.json exactly as the build scripts do', () => {
 for (const text of ['{"version":2,"minimum":0}', '{"version":1,"minimum":1}', '{"version":0,"minimum":0}', '{"version":2,"minimum":3}', '{"version":"2","minimum":0}', '{"version":2}', '[]', 'null', '{"version":1.5,"minimum":0}']) {
  let expected; try { expected = readLauncher(text); } catch { expected = 'error'; }
  let actual; try { actual = swap.parseLauncher(text); } catch { actual = 'error'; }
  assert.deepEqual(actual, expected, text);
 }
 assert.deepEqual(swap.parseLauncher(fs.readFileSync(path.join(realDesktop, 'launcher.json'), 'utf8')).version, 2);
});

test('the new archive holds exactly the packed launcher files from the update, with the installed package.json', t => {
 const f = install(t);
 const members = swap.launcherMembers(f.desktopDir, path.join(f.resources, 'app.asar'), fs);
 assert.deepEqual(members.map(m => m.name), ['package.json', 'dist/main/launcher-self-test.js', ENTRY, 'dist/renderer/app.js', 'src/pages/index.html', 'build/icon.png',
  'build/application-update-key.pem', 'scripts/source-profile.cjs', 'launcher.json', 'node_modules/yauzl/index.js', 'node_modules/@scope/lib/index.js']);
 // electron-builder rewrote the installed package.json and its name picks the profile folder: keep it byte for byte.
 assert.equal(members[0].data.toString(), INSTALLED_PACKAGE);
});

test('the new archive refuses links in the update, a different entry point and an update without the launcher files', t => {
 const f = install(t);
 const asar = path.join(f.resources, 'app.asar');
 const outside = path.join(f.dir, 'outside.js'); fs.writeFileSync(outside, 'planted');
 fs.symlinkSync(outside, path.join(f.desktopDir, 'dist/main/planted.js'));
 assert.throws(() => swap.launcherMembers(f.desktopDir, asar, fs), /link/);
 fs.unlinkSync(path.join(f.desktopDir, 'dist/main/planted.js'));
 fs.symlinkSync(f.dir, path.join(f.desktopDir, 'src/pages/dir-link'));
 assert.throws(() => swap.launcherMembers(f.desktopDir, asar, fs), /link/);
 fs.unlinkSync(path.join(f.desktopDir, 'src/pages/dir-link'));
 assert.doesNotThrow(() => swap.launcherMembers(f.desktopDir, asar, fs));

 const other = install(t, { desktop: fakeDesktop(tmp(t), { main: 'dist/main/index.js' }) });
 assert.throws(() => swap.launcherMembers(other.desktopDir, path.join(other.resources, 'app.asar'), fs), /entry points/);
 fs.writeFileSync(path.join(f.resources, 'app.asar'), packAsar([{ name: 'package.json', data: Buffer.from('{"main":"dist/main/index.js"}') }]));
 assert.throws(() => swap.launcherMembers(f.desktopDir, asar, fs), /entry points/);
 fs.writeFileSync(path.join(f.resources, 'app.asar'), f.original);
 fs.rmSync(path.join(f.desktopDir, 'dist/main/launcher-self-test.js'));
 assert.throws(() => swap.launcherMembers(f.desktopDir, asar, fs), /no dist\/main\/launcher-self-test\.js/);
});

/** A candidate folder named like an archive whose self-test script is `script`; node stands in for the EXE in node mode. */
function candidateWith(t, script) {
 const candidate = path.join(tmp(t), 'app.new.asar');
 write(candidate, { [swap.SELF_TEST]: script });
 return candidate;
}
const nonceOf = 'const nonce = process.argv.find(a => a.startsWith("--nonce=")).slice(8);';
test('self-test verdicts: only a clean exit with this run\'s verdict for the expected version passes', async t => {
 const root = tmp(t);
 process.env.LAUNCHER_SWAP_TEST_SECRET = 'must not reach the self-test';
 t.after(() => delete process.env.LAUNCHER_SWAP_TEST_SECRET);
 const ok = candidateWith(t, `${nonceOf}
  if (process.env.LAUNCHER_SWAP_TEST_SECRET || process.env.ELECTRON_RUN_AS_NODE !== '1') process.exit(9);
  console.log('noise'); console.log(JSON.stringify({ launcherSelfTest: 1, nonce, ok: true, version: 2 }));`);
 await swap.selfTest(process.execPath, ok, 2, root);
 const cases = {
  'an EXE that ignores the script and exits 0': ['', /no verdict/],
  'a verdict for another run': [`console.log(JSON.stringify({ launcherSelfTest: 1, nonce: 'f'.repeat(32), ok: true, version: 2 }));`, /no verdict/],
  'a passing verdict with a failing exit': [`${nonceOf} console.log(JSON.stringify({ launcherSelfTest: 1, nonce, ok: true, version: 2 })); process.exit(1);`, /exit 1/],
  'another launcher version': [`${nonceOf} console.log(JSON.stringify({ launcherSelfTest: 1, nonce, ok: true, version: 1 }));`, /failed/],
  'a failing verdict': [`${nonceOf} console.log(JSON.stringify({ launcherSelfTest: 1, nonce, ok: false, error: 'state.json unreadable' })); process.exitCode = 1;`, /state\.json unreadable/],
  'a crash': [`throw new Error('boom')`, /boom/],
 };
 for (const [name, [script, error]] of Object.entries(cases)) await assert.rejects(swap.selfTest(process.execPath, candidateWith(t, script), 2, root), error, name);
 await assert.rejects(swap.selfTest(process.execPath, candidateWith(t, 'setInterval(() => {}, 1000)'), 2, root, 300), /did not finish in time/);
 await assert.rejects(swap.selfTest(path.join(root, 'missing.exe'), ok, 2, root), /ENOENT/);
});

test('the real self-test passes from the real launcher files, alone in their archive, and checks this install\'s state with the archive\'s key', async t => {
 const f = install(t, { desktop: realDesktop });
 const members = swap.launcherMembers(realDesktop, path.join(f.resources, 'app.asar'), fs);
 const names = members.map(m => m.name);
 assert(names.includes('node_modules/yauzl/package.json') && names.includes('node_modules/pend/package.json'));
 assert(!names.some(name => /^node_modules\/(electron|typescript|@electron)\//.test(name)), 'no development dependencies');
 assert(names.includes('launcher.json') && names.includes(swap.SELF_TEST));
 // Node cannot open an archive; the same members in a folder named like one is what the EXE sees through Electron's asar support.
 const candidate = path.join(f.dir, 'isolated', 'app.new.asar');
 const { privateKey, publicKey } = generateKeyPairSync('ed25519');
 write(candidate, Object.fromEntries(members.map(m => [m.name, m.data])));
 await swap.selfTest(process.execPath, candidate, 2, f.root);
 await assert.rejects(swap.selfTest(process.execPath, candidate, 3, f.root), /different launcher version/);
 // A selected release that does not verify with the archive's key fails the test.
 const payload = Buffer.from(JSON.stringify({ format: 2, revision: B, sequence: 5, sha256: 'e'.repeat(64), size: 10, desktop: { launcher: 2 } }));
 write(f.root, { 'state.json': JSON.stringify({ active: A, pending: B }), [`versions/${B}/release.json`]: JSON.stringify({ payload: payload.toString('base64'), signature: sign(null, payload, privateKey).toString('base64') }) });
 await assert.rejects(swap.selfTest(process.execPath, candidate, 2, f.root), /signature/);
 write(candidate, { 'build/application-update-key.pem': publicKey.export({ type: 'spki', format: 'pem' }) });
 await swap.selfTest(process.execPath, candidate, 2, f.root);
 fs.writeFileSync(path.join(f.root, 'state.json'), '{"active":"not a revision"}');
 await assert.rejects(swap.selfTest(process.execPath, candidate, 2, f.root), /Invalid installed revision/);
 fs.rmSync(path.join(f.root, 'installed-host.json'));
 fs.rmSync(path.join(f.root, 'state.json'));
 await assert.rejects(swap.selfTest(process.execPath, candidate, 2, f.root), /No installed launcher record/);
 // Not from an archive: refused.
 const loose = path.join(f.dir, 'isolated', 'loose'); fs.cpSync(candidate, loose, { recursive: true });
 await assert.rejects(swap.selfTest(process.execPath, loose, 2, f.root), /not running from an archive/);
});

test('an older launcher than the release needs is swapped: app.new.asar, self-test, app.asar.old, trial record, rename', async t => {
 const f = install(t);
 let tested;
 const result = await swap.replaceLauncher(f.options({ selfTest: async (...args) => { tested = args; assert(f.has('app.new.asar')); assert(!f.has('app.asar.old')); } }));
 assert.equal(result, 'swapped');
 assert.deepEqual(tested, [f.exe, path.join(f.resources, 'app.new.asar'), 2, f.root]);
 const installed = path.join(f.resources, 'app.asar');
 assert.equal(JSON.parse(readAsarMember(fs, installed, 'launcher.json')).version, 2);
 assert.equal(readAsarMember(fs, installed, 'package.json').toString(), INSTALLED_PACKAGE);
 assert.equal(readAsarMember(fs, installed, ENTRY).toString(), 'new entry');
 assert.deepEqual(fs.readFileSync(path.join(f.resources, 'app.asar.old')), f.original);
 assert(!f.has('app.new.asar'));
 assert.deepEqual(f.marker(), { format: 1, phase: 'trial', revision: A, from: 1, to: 2, starts: 0, old: sha(f.original), new: sha(f.asar()) });
 // Only one swap at a time: until a start settles it, nothing else is tried.
 assert.equal(await swap.replaceLauncher(f.options({ selfTest: () => assert.fail('no second self-test') })), 'skipped');
});

test('no swap when the launcher is current, the release needs none, or the update has no newer launcher', async t => {
 const f = install(t);
 const never = () => assert.fail('nothing to test');
 assert.equal(await swap.replaceLauncher(f.options({ running: 2, selfTest: never })), 'current');
 assert.equal(await swap.replaceLauncher(f.options({ manifest: { revision: A }, running: 0, selfTest: never })), 'current');
 assert.equal(await swap.replaceLauncher(f.options({ manifest: { revision: A, desktop: { launcher: 3 } }, selfTest: never })), 'unavailable');
 assert.equal(await swap.replaceLauncher(f.options({ manifest: { revision: A, desktop: { launcher: 2 } }, running: 1, application: { revision: A, desktopDir: fakeDesktop(tmp(t), { version: 1 }) }, selfTest: never })), 'unavailable');
 await assert.rejects(swap.replaceLauncher(f.options({ manifest: { revision: B, desktop: { launcher: 2 } }, selfTest: never })), /does not match/);
 assert.deepEqual(f.asar(), f.original); assert(!f.has('app.new.asar')); assert(!f.has('app.asar.old')); assert.equal(f.marker(), null);
});

test('never swap when the self-test fails: the candidate is removed, app.asar untouched, and that revision is not tried again', async t => {
 const f = install(t);
 assert.equal(await swap.replaceLauncher(f.options({ selfTest: async () => { throw new Error('The launcher self-test failed (exit 1): yauzl missing'); } })), 'rejected');
 assert.deepEqual(f.asar(), f.original); assert(!f.has('app.new.asar')); assert(!f.has('app.asar.old'));
 const marker = f.marker();
 assert.equal(marker.phase, 'rejected'); assert.equal(marker.revision, A); assert.match(marker.reason, /yauzl missing/);
 assert.equal(await swap.replaceLauncher(f.options({ selfTest: () => assert.fail('not again for this revision') })), 'skipped');
 assert.equal(swap.launcherTrial(f.root, 2, fs), 'none');
 // A later release may try its own launcher.
 assert.equal(await swap.replaceLauncher(f.options({ application: { revision: B, desktopDir: f.desktopDir }, manifest: { revision: B, desktop: { launcher: 2 } } })), 'swapped');
});

test('never swap from an unverified release: the signed release.json must verify with the running key', async t => {
 const f = install(t);
 const { privateKey } = generateKeyPairSync('ed25519');
 const { publicKey: trusted } = generateKeyPairSync('ed25519');
 const payload = Buffer.from(JSON.stringify({ format: 2, revision: A, sequence: 5, sha256: 'e'.repeat(64), size: 10, desktop: { launcher: 2 } }));
 write(f.root, { [`versions/${A}/release.json`]: JSON.stringify({ payload: payload.toString('base64'), signature: sign(null, payload, privateKey).toString('base64') }) });
 const updates = new SourceUpdates({ root: f.root, publicKey: trusted.export({ type: 'spki', format: 'pem' }), bootstrapNode: process.execPath });
 await assert.rejects(updates.release(A), /signature/);
 let tested = false;
 const context = { updates, application: f.application, root: f.root, host: f.host };
 const first = swap.replaceInstalledLauncher(context, 1);
 await assert.rejects(first, /signature/);
 // Once per run: a failed attempt waits for the next start.
 assert.equal(swap.replaceInstalledLauncher({ ...context, updates: { release: async () => { tested = true; return f.manifest; } } }, 1), first);
 assert(!tested);
 assert.deepEqual(f.asar(), f.original); assert(!f.has('app.new.asar')); assert(!f.has('app.asar.old')); assert.equal(f.marker(), null);
});

test('paths cannot be redirected: a linked install folder or archive is refused, a planted link is removed, never followed', async t => {
 const f = install(t);
 assert.throws(() => swap.installedResources('AI Shuffle.exe', fs), /not a file/);
 // The install folder reached through a link (a junction on Windows).
 const real = path.join(f.dir, 'elsewhere');
 fs.renameSync(path.dirname(f.exe), real);
 fs.symlinkSync(real, path.dirname(f.exe));
 await assert.rejects(swap.replaceLauncher(f.options()), /link/);
 assert(!fs.existsSync(path.join(real, 'resources', 'app.new.asar')));
 fs.unlinkSync(path.dirname(f.exe)); fs.renameSync(real, path.dirname(f.exe));
 // app.asar itself a link.
 const target = path.join(f.dir, 'target.asar'); fs.writeFileSync(target, f.original);
 fs.renameSync(path.join(f.resources, 'app.asar'), path.join(f.dir, 'kept.asar'));
 fs.symlinkSync(target, path.join(f.resources, 'app.asar'));
 await assert.rejects(swap.replaceLauncher(f.options()), /not a file/);
 fs.unlinkSync(path.join(f.resources, 'app.asar')); fs.renameSync(path.join(f.dir, 'kept.asar'), path.join(f.resources, 'app.asar'));
 // Links planted where the swap writes are removed, and the files they pointed at are untouched.
 const victim = path.join(f.dir, 'victim.txt'); fs.writeFileSync(victim, 'keep me');
 fs.symlinkSync(victim, path.join(f.resources, 'app.new.asar'));
 fs.symlinkSync(victim, path.join(f.resources, 'app.asar.old.tmp'));
 assert.equal(await swap.replaceLauncher(f.options()), 'swapped');
 assert.equal(fs.readFileSync(victim, 'utf8'), 'keep me');
 assert(!fs.lstatSync(path.join(f.resources, 'app.asar.old')).isSymbolicLink());
 // A folder where the candidate goes stops the swap rather than being deleted.
 const g = install(t);
 fs.mkdirSync(path.join(g.resources, 'app.new.asar'));
 await assert.rejects(swap.replaceLauncher(g.options()), /is a directory/);
 assert.deepEqual(g.asar(), g.original);
});

test('a second launcher holding app.asar open (Windows) leaves everything as it was for a later start', async t => {
 const f = install(t);
 const busy = { ...fs, renameSync(from, to) { if (from.endsWith('app.new.asar')) throw Object.assign(new Error('busy'), { code: 'EBUSY' }); return fs.renameSync(from, to); } };
 assert.equal(await swap.replaceLauncher(f.options({ fs: busy })), 'busy');
 assert.deepEqual(f.asar(), f.original); assert(!f.has('app.new.asar')); assert.equal(f.marker(), null);
 assert.equal(swap.confirmLauncher(f.root, f.exe, 1, A, fs), 'none');
 assert.equal(await swap.replaceLauncher(f.options()), 'swapped');
});

for (const step of ['written', 'tested', 'kept', 'marked']) {
 test(`a swap interrupted after "${step}" keeps the old launcher working and is retried at a later start`, async t => {
  const f = install(t);
  await assert.rejects(swap.replaceLauncher(f.options({ interrupt: at => { if (at === step) throw new Error('power lost'); } })), /power lost/);
  // app.asar is the whole old launcher at every step.
  assert.deepEqual(f.asar(), f.original);
  // The old launcher is not on trial and starts the app as before.
  assert.equal(swap.launcherTrial(f.root, 1, fs), 'none');
  // The app, started by the old launcher, settles what was left.
  assert.equal(swap.confirmLauncher(f.root, f.exe, 1, A, fs), step === 'marked' ? 'retry' : 'none');
  assert.equal(f.marker(), null);
  if (step === 'marked') assert(!f.has('app.new.asar'));
  assert.equal(await swap.replaceLauncher(f.options()), 'swapped');
  assert.deepEqual(fs.readFileSync(path.join(f.resources, 'app.asar.old')), f.original);
  assert.equal(JSON.parse(readAsarMember(fs, path.join(f.resources, 'app.asar'), 'launcher.json')).version, 2);
 });
}

test('the new launcher\'s first start is a trial the updated app confirms; app.asar.old is kept until a later release runs', async t => {
 const f = install(t);
 assert.equal(await swap.replaceLauncher(f.options()), 'swapped');
 assert.equal(swap.launcherTrial(f.root, 1, fs), 'none');
 assert.equal(swap.launcherTrial(f.root, 2, fs), 'trial');
 swap.countLauncherStart(f.root, fs);
 assert.equal(f.marker().starts, 1);
 // The app it started reports launcher 2.
 assert.equal(swap.confirmLauncher(f.root, f.exe, 2, A, fs), 'confirmed');
 assert.equal(swap.launcherTrial(f.root, 2, fs), 'none');
 swap.countLauncherStart(f.root, fs);
 assert.equal(f.marker().starts, 1);
 assert.equal(swap.confirmLauncher(f.root, f.exe, 2, A, fs), 'confirmed');
 assert(f.has('app.asar.old'));
 assert.equal(await swap.replaceLauncher(f.options({ running: 2 })), 'current');
 // The next release retires the kept launcher.
 assert.equal(swap.confirmLauncher(f.root, f.exe, 2, B, fs), 'retired');
 assert(!f.has('app.asar.old')); assert.equal(f.marker(), null);
});

test('settling a swap: a reinstall clears it; an unexplained state is left alone; an unreadable record never stops the launcher', async t => {
 const f = install(t);
 await swap.replaceLauncher(f.options());
 const marker = f.marker();
 assert.equal(swap.confirmLauncher(f.root, f.exe, 1, A, fs), 'unknown');
 assert.deepEqual(f.marker(), marker);
 fs.writeFileSync(path.join(f.resources, 'app.asar'), packAsar([{ name: 'package.json', data: Buffer.from(INSTALLED_PACKAGE) }]));
 assert.equal(swap.confirmLauncher(f.root, f.exe, 1, A, fs), 'cleared');
 assert.equal(f.marker(), null);
 fs.writeFileSync(path.join(f.root, swap.SWAP_MARKER), '{"format":1,"phase":"trial"}');
 assert.equal(swap.launcherTrial(f.root, 2, fs), 'none');
 assert.throws(() => swap.readMarker(f.root, fs), /Invalid launcher swap record/);
 fs.writeFileSync(path.join(f.root, swap.SWAP_MARKER), 'not json');
 assert.equal(swap.launcherTrial(f.root, 2, fs), 'none');
});

/** A process that is still running for a moment, standing in for the launcher that must exit first. */
function briefProcess(ms) {
 const child = spawn(process.execPath, ['-e', `setTimeout(() => {}, ${ms})`], { stdio: 'ignore' });
 return { pid: child.pid, exited: new Promise(resolve => child.on('exit', resolve)) };
}
async function until(check, ms = 15000) {
 const end = Date.now() + ms;
 while (!check()) { if (Date.now() > end) throw new Error('timed out'); await new Promise(resolve => setTimeout(resolve, 50)); }
}

test('a new launcher that never reached the app is restored from app.asar.old after it exits, and the old one starts again', async t => {
 const f = install(t);
 await swap.replaceLauncher(f.options());
 assert.equal(swap.launcherTrial(f.root, 2, fs), 'trial');
 swap.countLauncherStart(f.root, fs);
 // The app never confirmed. Next start of the new launcher:
 assert.equal(swap.launcherTrial(f.root, 2, fs), 'restore');
 const launcher = briefProcess(700);
 swap.restoreLauncher(f.root, f.exe, launcher.pid, fs, process.execPath);
 assert.equal(f.marker().phase, 'restoring');
 // Still due if this start dies before the helper finishes.
 assert.equal(swap.launcherTrial(f.root, 2, fs), 'restore');
 // Not before the launcher exited (Windows keeps app.asar open while it runs).
 assert.notDeepEqual(f.asar(), f.original);
 await launcher.exited;
 await until(() => fs.existsSync(path.join(f.dir, 'Programs', 'AI Shuffle', 'relaunched')));
 assert.deepEqual(f.asar(), f.original);
 assert(!f.has('app.restore.tmp'));
 // The old launcher started the app again, which records the outcome; that revision's launcher is not tried again.
 assert.equal(swap.launcherTrial(f.root, 1, fs), 'none');
 assert.equal(swap.confirmLauncher(f.root, f.exe, 1, A, fs), 'restored');
 assert.equal(f.marker().phase, 'restored');
 assert.equal(await swap.replaceLauncher(f.options({ selfTest: () => assert.fail('not again') })), 'skipped');
});

test('the restore helper only copies the archive the swap kept, never a changed or linked one', t => {
 const f = install(t);
 const resources = f.resources;
 const run = expected => spawnSync(process.execPath, ['-e', swap.RESTORE_HELPER, '999999999', resources, expected, path.join(f.dir, 'no-such.exe')], { encoding: 'utf8' });
 const swapped = Buffer.from('new launcher');
 fs.writeFileSync(path.join(resources, 'app.asar'), swapped);
 fs.writeFileSync(path.join(resources, 'app.asar.old'), Buffer.from('changed after the swap'));
 assert.equal(run(sha(f.original)).status, 3);
 assert.deepEqual(f.asar(), swapped);
 const elsewhere = path.join(f.dir, 'elsewhere.asar'); fs.writeFileSync(elsewhere, f.original);
 fs.rmSync(path.join(resources, 'app.asar.old')); fs.symlinkSync(elsewhere, path.join(resources, 'app.asar.old'));
 assert.equal(run(sha(f.original)).status, 3);
 assert.deepEqual(f.asar(), swapped);
 // restoreLauncher does nothing without a swap on trial.
 swap.restoreLauncher(f.root, f.exe, 1, fs, path.join(f.dir, 'must-not-run'));
 assert.equal(f.marker(), null);
});

// Electron's own asar support and node mode, where its binary is installed (not on CI runners that skip the download).
let electron = null;
try { electron = require('electron'); if (typeof electron !== 'string' || !fs.existsSync(electron)) electron = null; } catch { electron = null; }
test('real Electron in node mode opens the packed app.new.asar and passes its self-test; a wrong archive fails', { skip: electron ? false : 'no Electron binary installed' }, async t => {
 const f = install(t, { desktop: realDesktop });
 const members = swap.launcherMembers(realDesktop, path.join(f.resources, 'app.asar'), fs);
 const candidate = path.join(f.resources, swap.NEW_ASAR);
 fs.writeFileSync(candidate, packAsar(members));
 await swap.selfTest(electron, candidate, 2, f.root);
 await assert.rejects(swap.selfTest(electron, candidate, 3, f.root), /different launcher version/);
 // The same bytes under the name the design first suggested are not opened as an archive.
 const misnamed = path.join(f.resources, 'app.asar.new');
 fs.copyFileSync(candidate, misnamed);
 await assert.rejects(swap.selfTest(electron, misnamed, 2, f.root), /failed/);
 // An archive without yauzl's dependency fails before any swap.
 fs.writeFileSync(candidate, packAsar(members.filter(m => !m.name.startsWith('node_modules/pend/'))));
 await assert.rejects(swap.selfTest(electron, candidate, 2, f.root), /pend/);
});
test('real Electron in node mode runs the restore helper over a real archive with original-fs', { skip: electron ? false : 'no Electron binary installed' }, async t => {
 const f = install(t);
 assert.equal(await swap.replaceLauncher(f.options()), 'swapped');
 const result = spawnSync(electron, ['-e', swap.RESTORE_HELPER, '999999999', f.resources, sha(f.original), f.exe], { env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' }, encoding: 'utf8' });
 assert.equal(result.status, 0, result.stderr);
 assert.deepEqual(f.asar(), f.original);
 await until(() => fs.existsSync(path.join(f.dir, 'Programs', 'AI Shuffle', 'relaunched')));
});
