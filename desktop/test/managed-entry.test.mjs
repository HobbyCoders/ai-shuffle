import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { test } from 'node:test';

const code = readFileSync(new URL('../dist/main/managed-entry.js', import.meta.url), 'utf8');
async function launch({ packaged, env = {} }) {
 const events = [];
 const module = { exports: {} };
 const app = { isPackaged: packaged, whenReady: async () => {}, quit: () => events.push('quit') };
 const updates = { attach: async revision => events.push(['attach', revision]), application: async () => ({}) };
 vm.runInNewContext(code, {
  module, exports: module.exports, __dirname: path.resolve('desktop/dist/main'), process: { platform: 'win32', env },
  require: id => {
   if (id === 'electron') return { app, dialog: { showErrorBox: (_title, error) => events.push(error) } };
   if (id === 'node:fs') return { promises: { readFile: async () => 'public-key' } };
   if (id === 'node:path') return path;
   if (id === './source-updates') return { SourceUpdates: class { constructor() { return updates; } } };
   if (id === './managed-context') return { setManagedContext: () => events.push('managed') };
   if (id === './source-profile') return { readProfile: () => null, localFeed: () => undefined };
   if (id === './installed-host') return { readInstalledHostSync: () => null };
   if (id === './installed-entry') return { startInstalled: async () => events.push('select-installed') };
   if (id === './index') { events.push('run'); return {}; }
   throw Error('Unexpected module ' + id);
  }
 });
 await new Promise(resolve => setImmediate(resolve));
 return events;
}

const sourceEnv = { SHUFFLE_SOURCE_ROOT: path.resolve('profile/updates'), SHUFFLE_SOURCE_REVISION: 'a'.repeat(40), SHUFFLE_SOURCE_NODE: path.resolve('runtime/node.exe') };
test('packaged restarts select pending updates instead of attaching an inherited old revision', async () => {
 assert.deepEqual(await launch({ packaged: true, env: sourceEnv }), ['select-installed']);
 assert.deepEqual(await launch({ packaged: true }), ['select-installed']);
});
test('unpackaged source launches still attach their selected revision and development launches normally', async () => {
 assert.deepEqual(await launch({ packaged: false, env: sourceEnv }), [['attach', sourceEnv.SHUFFLE_SOURCE_REVISION], 'managed', 'run']);
 assert.deepEqual(await launch({ packaged: false }), ['run']);
});
