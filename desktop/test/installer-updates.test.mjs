import assert from 'node:assert/strict';
import { createHash, generateKeyPairSync, sign } from 'node:crypto';
import { promises as fs, readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { test } from 'node:test';
const require = createRequire(import.meta.url);
const { InstallerUpdates, installerPlatform, isNewerVersion, verifyInstallerEntry, INSTALLERS_URL } = require('../dist/main/installer-updates.js');

const REVISION = 'b'.repeat(40);
const { privateKey, publicKey: key } = generateKeyPairSync('ed25519');
const publicKey = key.export({ type: 'spki', format: 'pem' });
const other = generateKeyPairSync('ed25519').privateKey;

/** A fake hub: the signed per-platform list and the installer bytes, recording each request. */
async function fixture(t, { version = '0.9.0', platform = 'mac', arch = 'arm64', bytes = Buffer.from('installer bytes'), signer = privateKey, fields = {} } = {}) {
 const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-installers-'));
 t.after(() => fs.rm(directory, { recursive: true, force: true }));
 const file = `AI-Shuffle-Desktop-${version}-${platform}-${arch}.dmg`;
 const payload = Buffer.from(JSON.stringify({ type: 'desktop-installer', format: 1, platform, arch, version, revision: REVISION, file, size: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex'), ...fields }));
 const entry = { platform, arch, version, revision: REVISION, file, url: 'https://evil.example/installer', size: 1, sha256: '0'.repeat(64), payload: payload.toString('base64'), signature: sign(null, payload, signer).toString('base64') };
 const list = { format: 1, version: '0.8.13', revision: 'a'.repeat(40), size: 9, sha256: 'e'.repeat(64), url: 'https://hobbycoders.com/v1/desktop/installer', platforms: { win: null, mac: null, linux: null, [platform]: entry } };
 const requests = []; const hub = { list, body: bytes, status: 200 };
 const fetch = async (url, init) => {
  requests.push([url, init]);
  if (url === INSTALLERS_URL) return new Response(JSON.stringify(hub.list));
  if (url === `https://hobbycoders.com/v1/desktop/installer/${platform}`) return new Response(hub.body, { status: hub.status });
  return new Response('not found', { status: 404 });
 };
 const updates = (options = {}) => new InstallerUpdates({ platform, arch, currentVersion: '0.8.18', publicKey, directory, fetch, ...options });
 return { directory, file, entry, hub, requests, updates };
}

test('installer checks apply to macOS and Linux only; Windows keeps its own path', () => {
 assert.equal(installerPlatform('darwin'), 'mac');
 assert.equal(installerPlatform('linux'), 'linux');
 assert.equal(installerPlatform('win32'), null);
 assert.equal(isNewerVersion('0.9.0', '0.8.18'), true);
 assert.equal(isNewerVersion('0.10.0', '0.9.9'), true);
 assert.equal(isNewerVersion('1.0.0', '0.99.99'), true);
 assert.equal(isNewerVersion('0.8.18', '0.8.18'), false);
 assert.equal(isNewerVersion('0.8.17', '0.8.18'), false);
 assert.equal(isNewerVersion('0.9.0', '0.9.0-dev.3+gabc1234'), false);
});

test('a newer signed entry for this platform and architecture is offered, and only the list is fetched', async t => {
 const f = await fixture(t);
 const result = await f.updates().check();
 assert.equal(result.kind, 'available');
 assert.equal(result.version, '0.9.0');
 assert.equal(result.release, '0.9.0');
 assert.equal(result.installer.file, f.file);
 assert.deepEqual(f.requests.map(([url]) => url), ['https://hobbycoders.com/v1/desktop/installers']);
});

test('the same, older, other-architecture and missing entries are not offered', async t => {
 for (const currentVersion of ['0.9.0', '0.9.1']) {
  const f = await fixture(t);
  assert.deepEqual(await f.updates({ currentVersion }).check(), { kind: 'current' });
 }
 const intel = await fixture(t);
 assert.deepEqual(await intel.updates({ arch: 'x64' }).check(), { kind: 'current' });
 const none = await fixture(t);
 none.hub.list.platforms.mac = null;
 assert.deepEqual(await none.updates().check(), { kind: 'current' });
 const linux = await fixture(t, { platform: 'linux', arch: 'x64' });
 assert.equal((await linux.updates().check()).kind, 'available');
 // The Linux entry is never taken for a Mac, even when signed.
 linux.hub.list.platforms.mac = linux.hub.list.platforms.linux;
 await assert.rejects(linux.updates({ platform: 'mac' }).check(), /Invalid installer entry/);
});

test('forged, unsigned, tampered and malformed entries are refused', async t => {
 const forged = await fixture(t, { signer: other });
 await assert.rejects(forged.updates().check(), /Invalid installer signature/);
 const f = await fixture(t);
 assert.throws(() => verifyInstallerEntry({ ...f.entry, signature: null }, publicKey, 'mac'), /not signed/);
 assert.throws(() => verifyInstallerEntry(null, publicKey, 'mac'), /not signed/);
 const tampered = JSON.parse(Buffer.from(f.entry.payload, 'base64').toString());
 tampered.version = '9.9.9';
 assert.throws(() => verifyInstallerEntry({ ...f.entry, payload: Buffer.from(JSON.stringify(tampered)).toString('base64') }, publicKey, 'mac'), /Invalid installer signature/);
 for (const fields of [{ type: 'desktop-source' }, { format: 2 }, { version: '0.9' }, { file: '../evil.dmg' }, { file: '.hidden' }, { size: 0 }, { sha256: 'x' }, { revision: 'main' }]) {
  const g = await fixture(t, { fields });
  await assert.rejects(g.updates().check(), /Invalid installer entry/, JSON.stringify(fields));
 }
 const unknown = await fixture(t);
 unknown.hub.list = { format: 2, platforms: {} };
 await assert.rejects(unknown.updates().check(), /Unknown installer list format/);
});

test('a download is checked against the signed size and checksum before it is kept', async t => {
 const f = await fixture(t);
 const updates = f.updates(); const progress = [];
 await updates.check();
 const saved = await updates.download((...args) => progress.push(args));
 assert.deepEqual(saved, { file: path.join(f.directory, f.file), version: '0.9.0' });
 assert.equal(await fs.readFile(saved.file, 'utf8'), 'installer bytes');
 assert.equal(progress.at(-1)[0], 100);
 // The bytes come from the hub's fixed address, never the entry's url field.
 assert.deepEqual(f.requests.map(([url]) => url), ['https://hobbycoders.com/v1/desktop/installers', 'https://hobbycoders.com/v1/desktop/installer/mac']);
 assert.deepEqual(await fs.readdir(f.directory), [f.file]);

 for (const body of [Buffer.from('installer byteZ'), Buffer.from('installer bytes, and more'), Buffer.from('short')]) {
  const g = await fixture(t);
  g.hub.body = body;
  const u = g.updates(); await u.check();
  await assert.rejects(u.download(), /signed (size|release)/);
  assert.deepEqual(await fs.readdir(g.directory), [], 'nothing unverified is left behind');
 }
 const paused = await fixture(t);
 paused.hub.status = 503;
 await assert.rejects(paused.updates().download(), /Installer request failed \(503\)/);
 const current = await fixture(t);
 await assert.rejects(current.updates({ currentVersion: '0.9.0' }).download(), /No newer installer/);
});

test('the default transport refuses redirects and sends no credentials', async t => {
 const f = await fixture(t);
 const module = { exports: {} }; const seen = [];
 const localRequire = createRequire(new URL('../dist/main/installer-updates.js', import.meta.url));
 vm.runInNewContext(readFileSync(new URL('../dist/main/installer-updates.js', import.meta.url), 'utf8'), {
  exports: module.exports, module, require: localRequire, Buffer, AbortSignal, process, Response,
  fetch: async (url, init) => { seen.push([url, init]); return new Response(JSON.stringify(f.hub.list)); }
 });
 const updates = new module.exports.InstallerUpdates({ platform: 'mac', arch: 'arm64', currentVersion: '0.8.18', publicKey, directory: f.directory });
 assert.equal((await updates.check()).kind, 'available');
 assert.equal(seen[0][0], 'https://hobbycoders.com/v1/desktop/installers');
 assert.equal(seen[0][1].redirect, 'error');
 assert.equal(seen[0][1].credentials, 'omit');
});
