import assert from 'node:assert/strict';
import { test } from 'node:test';
import { releaseRepository } from '../scripts/release-repository.mjs';

const pinned = { RELEASE_GITHUB_REPO_ID: '42' };
const identity = () => Response.json({ id: 42, full_name: 'HobbyCoders/ai-shuffle' });
const timeout = () => new DOMException('The operation was aborted due to timeout', 'TimeoutError');
const connectTimeout = () => new TypeError('fetch failed', { cause: { code: 'UND_ERR_CONNECT_TIMEOUT' } });

function retryFixture(t, responses) {
 const calls = [], waits = [];
 t.mock.method(globalThis, 'setTimeout', (callback, delay) => { waits.push(delay); callback(); });
 t.mock.method(globalThis, 'fetch', async (url, options) => {
  calls.push([url, options]);
  const result = responses.shift();
  if (result instanceof Error) throw result;
  return result;
 });
 return { calls, waits };
}

for (const [name, failure] of [
 ['HTTP 502', () => new Response(null, { status: 502 })],
 ['request timeout', timeout],
 ['fetch transport timeout', connectTimeout],
 ['headers timeout', () => new TypeError('fetch failed', { cause: { code: 'UND_ERR_HEADERS_TIMEOUT' } })],
 ['body timeout', () => new TypeError('terminated', { cause: { code: 'UND_ERR_BODY_TIMEOUT' } })],
 ...['UND_ERR_SOCKET', 'ECONNRESET', 'EPIPE', 'UND_ERR_CLOSED'].map(code =>
  [code, () => new TypeError('fetch failed', { cause: { code } })]),
]) {
 test(`repository identity recovers after ${name}`, async t => {
  const { calls, waits } = retryFixture(t, [failure(), identity()]);
  assert.deepEqual(await releaseRepository('fixture', pinned), { repository: 'HobbyCoders/ai-shuffle', id: 42 });
  assert.equal(calls.length, 2);
  assert.deepEqual(waits, [1000]);
  for (const [url, options] of calls) {
   assert.equal(url, 'https://api.github.com/repos/HobbyCoders/ai-shuffle');
   assert.equal(options.method, 'GET');
   assert.equal(options.redirect, 'manual');
   assert.equal(options.signal.aborted, false);
  }
  assert.notEqual(calls[0][1].signal, calls[1][1].signal);
 });
 test(`repository identity fails closed after exhausted ${name}`, async t => {
  const last = failure();
  const { calls, waits } = retryFixture(t, [failure(), failure(), last]);
  await assert.rejects(releaseRepository('fixture', pinned), error =>
   last instanceof Error ? error === last : /Release repository metadata unavailable/.test(error.message));
  assert.equal(calls.length, 3);
  assert.deepEqual(waits, [1000, 2000]);
 });
}

for (const status of [301, 302, 403, 404, 429]) {
 test(`repository identity does not retry HTTP ${status}`, async t => {
  const { calls, waits } = retryFixture(t, [new Response(null, { status })]);
  await assert.rejects(releaseRepository('fixture', pinned), /Release repository metadata unavailable/);
  assert.equal(calls.length, 1);
  assert.deepEqual(waits, []);
 });
}

for (const [name, failure, message] of [
 ['wrong repository', () => Response.json({ id: 42, full_name: 'someone/else' }), /identity mismatch/],
 ['wrong numeric identity', () => Response.json({ id: 43, full_name: 'HobbyCoders/ai-shuffle' }), /must pin/],
 ['malformed JSON', () => new Response('not json'), /JSON/],
 ['authentication error', () => new Error('authentication failed'), /authentication failed/],
 ['connection refused', () => new TypeError('fetch failed', { cause: { code: 'ECONNREFUSED' } }), /fetch failed/],
 ['DNS failure', () => new TypeError('fetch failed', { cause: { code: 'ENOTFOUND' } }), /fetch failed/],
 ['certificate error', () => new TypeError('fetch failed', { cause: { code: 'CERT_HAS_EXPIRED' } }), /fetch failed/],
 ['unclassified fetch failure', () => new TypeError('fetch failed'), /fetch failed/],
 ['unclassified body termination', () => new TypeError('terminated'), /terminated/],
 ['body termination with refused connection', () => new TypeError('terminated', { cause: { code: 'ECONNREFUSED' } }), /terminated/],
 ['non-fetch socket error', () => new Error('socket bug', { cause: { code: 'UND_ERR_SOCKET' } }), /socket bug/],
]) {
 test(`repository identity does not retry ${name}`, async t => {
  const { calls, waits } = retryFixture(t, [failure()]);
  await assert.rejects(releaseRepository('fixture', pinned), message);
  assert.equal(calls.length, 1);
  assert.deepEqual(waits, []);
 });
}

test('repository identity retries a timeout while reading the response body', async t => {
 const error = new TypeError('terminated', { cause: { code: 'UND_ERR_BODY_TIMEOUT' } });
 const partial = { status: 200, json: async () => { throw error; } };
 const { calls, waits } = retryFixture(t, [partial, identity()]);
 assert.deepEqual(await releaseRepository('fixture', pinned), { repository: 'HobbyCoders/ai-shuffle', id: 42 });
 assert.equal(calls.length, 2);
 assert.deepEqual(waits, [1000]);
});

for (const failures of [1, 3]) {
 test(`repository identity body socket close: ${failures === 1 ? 'recovers' : 'fails closed'}`, async t => {
  const error = new TypeError('terminated', { cause: { code: 'UND_ERR_SOCKET' } });
  const partial = { status: 200, json: async () => { throw error; } };
  const { calls, waits } = retryFixture(t, [...Array(failures).fill(partial), identity()]);
  if (failures === 1) {
   assert.deepEqual(await releaseRepository('fixture', pinned), { repository: 'HobbyCoders/ai-shuffle', id: 42 });
  } else {
   await assert.rejects(releaseRepository('fixture', pinned), failure => failure === error);
  }
  assert.equal(calls.length, failures === 1 ? 2 : 3);
  assert.deepEqual(waits, failures === 1 ? [1000] : [1000, 2000]);
 });
}

test('repository identity still validates the successful retry response', async t => {
 const { calls, waits } = retryFixture(t, [new Response(null, { status: 503 }),
  Response.json({ id: 43, full_name: 'HobbyCoders/ai-shuffle' })]);
 await assert.rejects(releaseRepository('fixture', pinned), /must pin/);
 assert.equal(calls.length, 2);
 assert.deepEqual(waits, [1000]);
});

test('publication repository identity is pinned, never redirected or guessed',async t=>{
 const original=globalThis.fetch;t.after(()=>globalThis.fetch=original);
 let calls=[];
 globalThis.fetch=async(url,options)=>{calls.push([url,options]);return Response.json({id:42,full_name:'HobbyCoders/ai-shuffle'});};
 const env={RELEASE_GITHUB_REPO_ID:'42'};
 assert.deepEqual(await releaseRepository('fixture',env),{repository:'HobbyCoders/ai-shuffle',id:42});
 assert.equal(calls[0][1].redirect,'manual');
 await assert.rejects(releaseRepository('fixture',{}),/must pin/);
 await assert.rejects(releaseRepository('fixture',{RELEASE_GITHUB_REPO_ID:'7'}),/must pin/);
 await assert.rejects(releaseRepository('fixture',{...env,RELEASE_GITHUB_REPO:'attacker/repo'}),/Unapproved/);
 // The archived private repository no longer publishes anything.
 await assert.rejects(releaseRepository('fixture',{...env,RELEASE_GITHUB_REPO:'HobbyCoders/ai-shuffle-private'}),/Unapproved/);
 await assert.rejects(releaseRepository('',env),/credentials/);
 globalThis.fetch=async()=>new Response(null,{status:301,headers:{Location:'https://example.com'}});
 await assert.rejects(releaseRepository('fixture',env),/metadata unavailable/);
});
