// Layer fault injection over the existing fully offline publisher fixture.
import './publish-source-preload.mjs';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import cp from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { createHash } from 'node:crypto';
import path from 'node:path';

const root = process.env.RUNNER_TEMP;
const { target, failure, failures = 1, ui = false } = JSON.parse(process.env.PUBLISH_READ_TEST);
const revision = 'a'.repeat(40), version = '0.8.18-dev.4+gaaaaaaa';
const uiBytes = Buffer.from('immutable UI ZIP fixture');
const uiHash = createHash('sha256').update(uiBytes).digest('hex');
const uiAsset = { id: 456, name: `${revision}-${uiHash}-ui.zip`, size: uiBytes.length,
 state: 'uploaded', digest: `sha256:${uiHash}` };
let inspectingUI = false;
if (ui) {
 process.env.SOURCE_UI_BUNDLE_FILE = path.join(root, 'ui.zip');
 fs.writeFileSync(process.env.SOURCE_UI_BUNDLE_FILE, uiBytes);
 const exec = cp.execFileSync;
 cp.execFileSync = (exe, args, options) => {
  if (exe === 'python' && args.includes('--inspect-ui')) {
   inspectingUI = true;
   return JSON.stringify({ revision, version, format: 1, sha256: uiHash, size: uiBytes.length });
  }
  return exec(exe, args, options);
 };
 syncBuiltinESMExports();
}

const calls = [], waits = [], signals = new Set();
globalThis.setTimeout = (callback, delay) => { waits.push(delay); callback(); };
const fetch = globalThis.fetch;
let attempts = 0;
globalThis.fetch = async (url, options = {}) => {
 const address = String(url), method = options.method || 'GET';
 const kind = address.startsWith('https://uploads.github.com/')
  ? (address.includes('-ui.zip') ? 'ui-upload' : 'source-upload')
  : address.endsWith('/releases/tags/desktop-sources') ? 'release'
  : address.endsWith('/releases') && method === 'POST' ? 'create'
  : address.includes('/releases/9/assets?') ? (inspectingUI ? 'ui-assets' : 'source-assets')
  : 'identity';
 calls.push({ kind, method, address });
 if (kind === target && (!kind.endsWith('assets') || address.endsWith('page=2'))) {
  attempts++;
  if (method === 'GET' && options.signal) {
   assert(!signals.has(options.signal), 'each attempt needs a fresh timeout signal');
   assert.equal(options.signal.aborted, false);
   signals.add(options.signal);
  }
  if (attempts <= failures) {
   if (kind.endsWith('upload')) {
    for await (const chunk of options.body) { /* consume before returning or failing */ }
   }
   if (failure === 'body-timeout' || failure === 'body-socket') return { ok: true, status: 200, json: async () => {
    throw new TypeError('terminated', { cause: { code: failure === 'body-socket' ? 'UND_ERR_SOCKET' : 'UND_ERR_BODY_TIMEOUT' } });
   } };
   if (failure === 'timeout') throw new DOMException('request timed out', 'TimeoutError');
   if (failure === 'socket') throw new TypeError('fetch failed', { cause: { code: 'UND_ERR_SOCKET' } });
   if (failure === 'refused') throw new TypeError('fetch failed', { cause: { code: 'ECONNREFUSED' } });
   return new Response(null, { status: failure });
  }
 }
 // Exercise pagination too: the second page is the failing read under test.
 if (kind === 'source-assets' || kind === 'ui-assets') {
  if (address.endsWith('page=1')) return Response.json(Array.from({ length: 100 }, (_, id) => ({ id, name: `other-${id}` })));
  if (ui && kind === 'ui-assets' && target !== 'ui-upload') return Response.json([uiAsset]);
  return Response.json([]);
 }
 if (kind === 'ui-upload') {
  for await (const chunk of options.body) { /* consume the upload stream */ }
  return Response.json(uiAsset);
 }
 return fetch(url, options);
};
process.on('exit', () => fs.writeFileSync(path.join(root, 'read-retries.json'), JSON.stringify({ calls, waits, signalCount: signals.size })));
