import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const script = fileURLToPath(new URL('../scripts/publish-source.mjs', import.meta.url));
const preload = new URL('./helpers/publish-source-retry-preload.mjs', import.meta.url).href;
async function publish(t, fault) {
 const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-read-retry-'));
 t.after(() => fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
 const env = { ...process.env, RUNNER_TEMP: root, GITHUB_SHA: 'a'.repeat(40), GH_TOKEN: 'fixture',
  RELEASE_GITHUB_REPO_ID: '42', ACCOUNT_HUB_DATABASE_ID: '11111111-1111-1111-1111-111111111111',
  PUBLISH_TEST_CASE: 'new', SOURCE_CHANNEL: 'nightly', PUBLISH_READ_TEST: JSON.stringify(fault) };
 const result = spawnSync(process.execPath, ['--import', preload, script], { encoding: 'utf8', env, timeout: 10000 });
 assert.ifError(result.error);
 const evidence = JSON.parse(await fs.readFile(path.join(root, 'read-retries.json'), 'utf8'));
 const events = JSON.parse(await fs.readFile(path.join(root, 'events.json'), 'utf8'));
 return { ...result, ...evidence, events, root };
}
const attempts = (calls, target) => calls.filter(call => call.kind === target &&
 (!target.endsWith('assets') || call.address.endsWith('page=2')));
const messages = { release: 'Source release lookup failed', 'source-assets': 'Source asset lookup failed', 'ui-assets': 'UI asset lookup failed',
 create: 'Source release lookup failed', 'source-upload': 'Source upload failed', 'ui-upload': 'UI asset upload failed' };

for (const target of ['release', 'source-assets', 'ui-assets']) {
 for (const failure of [502, 'timeout', 'socket', 'body-timeout', 'body-socket']) {
  test(`${target} recovers from one ${failure} with a fresh GET`, async t => {
   const result = await publish(t, { target, failure, ui: target === 'ui-assets' });
   assert.equal(result.status, 0, result.stderr);
   assert.equal(attempts(result.calls, target).length, 2);
   assert.deepEqual(result.waits, [1000]);
   assert.equal(result.signalCount, 2);
   assert(result.events.includes('publish'));
   assert.equal(result.calls.filter(call => call.kind === 'create').length, 1, '404 still creates once');
   assert.equal(result.calls.filter(call => call.kind === 'source-upload').length, 1);
  });
  test(`${target} fails closed after three ${failure} failures`, async t => {
   const result = await publish(t, { target, failure, failures: 3, ui: target === 'ui-assets' });
   assert.equal(result.status, 1, result.stderr);
   assert.equal(attempts(result.calls, target).length, 3);
   assert.deepEqual(result.waits, [1000, 2000]);
   assert.equal(result.signalCount, 3);
   if (failure === 502) assert(result.stderr.includes(`${messages[target]} (502)`));
   else assert.match(result.stderr, failure === 'timeout' ? /request timed out/ : failure.startsWith('body-') ? /terminated/ : /fetch failed/);
   assert(!result.events.includes('publish'));
   assert(!result.calls.some(call => call.kind.endsWith('upload')));
   await assert.rejects(fs.stat(path.join(result.root, 'record.json')), { code: 'ENOENT' });
  });
 }
 for (const failure of [403, 429, 'refused', ...(target === 'release' ? [] : [404])]) {
  test(`${target} does not retry ${failure}`, async t => {
   const result = await publish(t, { target, failure, ui: target === 'ui-assets' });
   assert.equal(result.status, 1, result.stderr);
   assert.equal(attempts(result.calls, target).length, 1);
   assert.deepEqual(result.waits, []);
   if (typeof failure === 'number') assert(result.stderr.includes(`${messages[target]} (${failure})`));
   assert(!result.events.includes('publish'));
  });
 }
}

test('release 404 still creates once without retrying the GET', async t => {
 const result = await publish(t, { target: 'release', failure: 404 });
 assert.equal(result.status, 0, result.stderr);
 assert.equal(attempts(result.calls, 'release').length, 1);
 assert.equal(attempts(result.calls, 'create').length, 1);
 assert.equal(attempts(result.calls, 'create')[0].method, 'POST');
 assert.deepEqual(result.waits, []);
 assert(result.events.includes('publish'));
});

for (const target of ['create', 'source-upload', 'ui-upload']) {
 for (const failure of [502, 'timeout', 'socket']) {
  test(`${target} never retries a write after ${failure}`, async t => {
   const result = await publish(t, { target, failure, ui: target === 'ui-upload' });
   assert.equal(result.status, 1, result.stderr);
   assert.equal(attempts(result.calls, target).length, 1);
   assert.equal(attempts(result.calls, target)[0].method, 'POST');
   assert.deepEqual(result.waits, []);
   if (typeof failure === 'number') assert(result.stderr.includes(`${messages[target]} (${failure})`));
   assert(!result.events.includes('publish'));
  });
 }
}
