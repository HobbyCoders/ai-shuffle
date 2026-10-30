import assert from 'node:assert/strict';
import { test } from 'node:test';
import { generateKeyPairSync, createHash, sign } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const { prepareUIBundle, publicUIFetch } = require('../dist/main/source-ui-bundle.js');
const { verifySourceManifest } = require('../dist/main/source-updates.js');
const extractor = fileURLToPath(new URL('./helpers/source-archive.py', import.meta.url));
const scripts = fileURLToPath(new URL('../../scripts', import.meta.url));
const B = 'b'.repeat(40), VERSION = '1.1.0-dev.1';

async function fixture(t, innerRevision = B, rootRevision = B) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-signed-ui-'));
  t.after(() => fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
  const build = path.join(root, 'input', `ai-shuffle-${rootRevision}`);
  await fs.mkdir(path.join(build, 'mobile'), { recursive: true });
  await fs.writeFile(path.join(build, 'index.html'), 'verified desktop');
  await fs.writeFile(path.join(build, 'mobile/index.html'), 'verified phone');
  await fs.writeFile(path.join(build, 'ui-build.json'), JSON.stringify({ format: 1, revision: innerRevision, version: VERSION }));
  const archive = path.join(root, 'ui.zip');
  execFileSync(process.env.PYTHON || 'python', [extractor, 'pack', path.dirname(build), archive]);
  const bytes = await fs.readFile(archive);
  const tree = execFileSync(process.env.PYTHON || 'python', ['-c',
    'import sys; sys.path.insert(0,sys.argv[1]); import source_archive as s; print(s.tree_digest(s.archive_files(sys.argv[2])))', scripts, archive], { encoding: 'utf8' }).trim();
  const ui = { format: 1, revision: B, version: VERSION, sha256: createHash('sha256').update(bytes).digest('hex'), size: bytes.length, tree };
  const stage = path.join(root, 'stage'); await fs.mkdir(stage);
  const { privateKey, publicKey } = generateKeyPairSync('ed25519');
  const verified = change => {
    const payload = Buffer.from(JSON.stringify({ format: 2, revision: B, sequence: 2, sha256: 'a'.repeat(64), size: 1, channel: 'stable', version: VERSION, tree: 'd'.repeat(64), ui: { ...ui, ...change } }));
    return verifySourceManifest(JSON.stringify({ payload: payload.toString('base64'), signature: sign(null, payload, privateKey).toString('base64') }), publicKey.export({ type: 'spki', format: 'pem' }));
  };
  const calls = [];
  const request = async (url, options) => { calls.push({ url, options }); return new Response(bytes); };
  return { stage, verified, request, calls };
}

test('a signed bundle yields both expected UI shells, with anonymous bounded download', async t => {
  const f = await fixture(t);
  const accepted = await prepareUIBundle(f.stage, f.verified(), f.request);
  assert.equal(await fs.readFile(path.join(accepted, 'index.html'), 'utf8'), 'verified desktop');
  assert.equal(await fs.readFile(path.join(accepted, 'mobile/index.html'), 'utf8'), 'verified phone');
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].options.credentials, 'omit');
  assert.ok(f.calls[0].url.startsWith('https://github.com/HobbyCoders/ai-shuffle/releases/download/desktop-sources/'));
});

for (const failure of ['sha256', 'tree', 'revision', 'version', 'size', 'inner-revision', 'root-revision', 'offline']) {
  test(`unusable UI cannot be activated and leaves local-build fallback clean: ${failure}`, async t => {
    const f = await fixture(t, failure === 'inner-revision' ? 'c'.repeat(40) : B, failure === 'root-revision' ? 'c'.repeat(40) : B);
    const changed = { sha256: 'f'.repeat(64), tree: 'f'.repeat(64), revision: 'c'.repeat(40), version: '9.9.9', size: 1 };
    const manifest = f.verified(failure in changed ? { [failure]: changed[failure] } : {});
    const result = await prepareUIBundle(f.stage, manifest, failure === 'offline' ? async () => { throw Error('offline'); } : f.request);
    assert.equal(result, undefined);
    assert.deepEqual(await fs.readdir(f.stage), []);
  });
}

test('the UI kill switch retains local compilation without making an artifact request', async t => {
  const f = await fixture(t);
  const old = process.env.AI_SHUFFLE_PERF_PREBUILT_UI;
  t.after(() => { if (old === undefined) delete process.env.AI_SHUFFLE_PERF_PREBUILT_UI; else process.env.AI_SHUFFLE_PERF_PREBUILT_UI = old; });
  process.env.AI_SHUFFLE_PERF_PREBUILT_UI = '0';
  assert.equal(await prepareUIBundle(f.stage, f.verified(), f.request), undefined);
  assert.deepEqual(f.calls, []);
  assert.deepEqual(await fs.readdir(f.stage), []);
});

test('the dedicated artifact transport refuses non-release origins and unsafe redirects', async t => {
  await assert.rejects(publicUIFetch('https://example.invalid/not-an-update'), /Invalid UI asset URL/);
  const old = globalThis.fetch;
  t.after(() => { globalThis.fetch = old; });
  const calls = [];
  globalThis.fetch = async (url, options) => { calls.push({ url, options }); return new Response('', { status: 302, headers: { location: 'https://example.invalid/redirect' } }); };
  await assert.rejects(publicUIFetch(`https://github.com/HobbyCoders/ai-shuffle/releases/download/desktop-sources/${B}-${'d'.repeat(64)}-ui.zip`), /Invalid UI asset redirect/);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.credentials, 'omit');
  assert.equal(calls[0].options.redirect, 'manual');
});
