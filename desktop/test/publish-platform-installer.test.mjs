import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash, verify } from 'node:crypto';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const script = fileURLToPath(new URL('../scripts/publish-platform-installer.mjs', import.meta.url));
const preload = new URL('./helpers/publish-platform-installer-preload.mjs', import.meta.url).href;
const revision = 'a'.repeat(40);
const apk = Buffer.from('signed universal release APK fixture');
const apkSha = createHash('sha256').update(apk).digest('hex');

// The files android/ci/prepare-apk-download.py writes, as android.yml uploads them.
async function androidArtifact(dir, { versionName = '1.2.3', commit = revision, recorded = apkSha, sums = apkSha, extra = [] } = {}) {
  await fs.mkdir(dir, { recursive: true });
  await fs.writeFile(path.join(dir, 'AI-Shuffle-Android.apk'), apk);
  await fs.writeFile(path.join(dir, 'AI-Shuffle-Android.apk.sha256'), `${sums}  AI-Shuffle-Android.apk\n`);
  await fs.writeFile(path.join(dir, 'android-build.json'), JSON.stringify({ versionName, versionCode: 29000000, sha256: recorded, commit, runNumber: 7, runUrl: 'https://github.com/HobbyCoders/ai-shuffle/actions/runs/1' }));
  for (const name of extra) await fs.writeFile(path.join(dir, name), apk);
}

async function run(t, platform, prepare, world = {}) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-platform-publish-test-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const dir = path.join(root, 'artifact');
  const asset = await prepare(dir);
  const env = {
    ...process.env, RUNNER_TEMP: root, GH_TOKEN: 'fixture', RELEASE_GITHUB_REPO_ID: '42', ACCOUNT_HUB_DATABASE_ID: '11111111-1111-1111-1111-111111111111',
    PLATFORM_TEST: JSON.stringify({ revision, asset, ...world }),
  };
  const result = spawnSync(process.execPath, ['--import', preload, script, platform, dir, 'v1.2.3'], { encoding: 'utf8', env });
  const events = JSON.parse(await fs.readFile(path.join(root, 'events.json'), 'utf8').catch(() => '[]'));
  const record = await fs.readFile(path.join(root, 'record.json'), 'utf8').then(JSON.parse, () => null);
  return { result, events, record, publicKey: await fs.readFile(path.join(root, 'public.pem'), 'utf8').catch(() => null) };
}

const apkAsset = (overrides = {}) => ({ id: 77, name: 'AI-Shuffle-Android.apk', state: 'uploaded', size: apk.length, digest: 'sha256:' + apkSha, ...overrides });

test('publishes the release APK as the signed android entry', async t => {
  const { result, record, publicKey, events } = await run(t, 'android', async dir => { await androidArtifact(dir); return apkAsset(); });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /Published android 1\.2\.3: AI-Shuffle-Android\.apk/);
  assert(events.includes('on-main'));
  assert.deepEqual({ ...record, payload: undefined, signature: undefined }, {
    release_repository: 'HobbyCoders/ai-shuffle', release_repository_id: 42,
    platform: 'android', version: '1.2.3', revision, arch: 'universal', file_name: 'AI-Shuffle-Android.apk', asset_id: 77, size: apk.length, sha256: apkSha, payload: undefined, signature: undefined,
  });
  const payload = Buffer.from(record.payload, 'base64');
  assert(verify(null, payload, publicKey, Buffer.from(record.signature, 'base64')));
  assert.deepEqual(JSON.parse(payload), { type: 'desktop-installer', format: 1, platform: 'android', arch: 'universal', version: '1.2.3', revision, file: 'AI-Shuffle-Android.apk', size: apk.length, sha256: apkSha });
});

const refusals = [
  ['an APK whose versionName is not the tag (a main build)', dir => androidArtifact(dir, { versionName: '0.8.18' }), /APK versionName 0\.8\.18 is not 1\.2\.3/],
  ['an APK whose versionName only starts with the tag', dir => androidArtifact(dir, { versionName: '1.2.3-dev.4+gabc' }), /APK versionName .* is not 1\.2\.3/],
  ['an APK built from another commit', dir => androidArtifact(dir, { commit: 'b'.repeat(40) }), /APK was built from b{40}/],
  ['an APK whose bytes differ from its provenance', dir => androidArtifact(dir, { recorded: 'c'.repeat(64) }), /does not match its build provenance/],
  ['an APK whose checksum file disagrees', dir => androidArtifact(dir, { sums: 'd'.repeat(64) }), /does not match AI-Shuffle-Android\.apk\.sha256/],
  ['an artifact with more than one APK', dir => androidArtifact(dir, { extra: ['app-debug.apk'] }), /Expected exactly one \.apk/],
];
for (const [name, prepare, error] of refusals) {
  test(`refuses ${name}`, async t => {
    const { result, record, events } = await run(t, 'android', async dir => { await prepare(dir); return apkAsset(); });
    assert.equal(result.status, 1);
    assert.match(result.stderr, error);
    assert.equal(record, null);
    assert(!events.includes('publish'));
    assert(!events.some(url => url.startsWith('https://api.github.com/')), 'nothing is looked up before the artifact is verified');
  });
}

test('refuses an uploaded APK asset that is not the verified bytes', async t => {
  const { result, record } = await run(t, 'android', async dir => { await androidArtifact(dir); return apkAsset({ digest: 'sha256:' + 'e'.repeat(64) }); });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Uploaded installer did not match verified bytes/);
  assert.equal(record, null);
});

test('refuses to replace a newer published APK', async t => {
  const { result, record } = await run(t, 'android', async dir => { await androidArtifact(dir); return apkAsset(); }, { oldVersion: '1.3.0' });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Refusing to replace the newer android installer 1\.3\.0/);
  assert.equal(record, null);
});

test('still publishes a Linux build from its SHA256SUMS and build provenance', async t => {
  const image = Buffer.from('linux AppImage fixture'), sha256 = createHash('sha256').update(image).digest('hex');
  const file = 'AI-Shuffle-Desktop-1.2.3-linux-x86_64.AppImage';
  const { result, record } = await run(t, 'linux', async dir => {
    await fs.mkdir(dir, { recursive: true });
    await fs.writeFile(path.join(dir, file), image);
    await fs.writeFile(path.join(dir, 'SHA256SUMS'), `${sha256}  ${file}\n`);
    await fs.writeFile(path.join(dir, 'linux-build-provenance.json'), JSON.stringify({ version: '1.2.3', sourceCommit: revision, artifacts: [{ name: file, bytes: image.length, sha256 }] }));
    return { id: 88, name: file, state: 'uploaded', size: image.length, digest: 'sha256:' + sha256 };
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(record.platform, 'linux');
  assert.equal(record.arch, 'x64');
  assert.equal(record.asset_id, 88);
});

test('rejects an unknown platform', async t => {
  const { result } = await run(t, 'ios', async () => ({}));
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Usage: publish-platform-installer\.mjs <mac\|linux\|android>/);
});
