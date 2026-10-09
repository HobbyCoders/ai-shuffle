import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const code = readFileSync(new URL('../dist/main/managed-entry.js', import.meta.url), 'utf8');
async function launch({ packaged, env = {}, platform = 'win32', asMain = true, direct = false }) {
 const events = [];
 const module = { exports: {} };
 const app = { isPackaged: packaged, whenReady: async () => {}, quit: () => events.push('quit') };
 const updates = { attach: async revision => events.push(['attach', revision]), application: async () => ({node:path.resolve('runtime/node/bin/node'),python:path.resolve('runtime/python/bin/python3')}) };
 const buildModule={exports:{}};vm.runInNewContext(readFileSync(new URL('../dist/main/source-build.js',import.meta.url),'utf8'),{module:buildModule,exports:buildModule.exports,require:createRequire(import.meta.url),process:{env}});
 vm.runInNewContext(code, {
  module, exports: module.exports, __dirname: path.resolve('desktop/dist/main'), process: { platform, env },
  require: Object.assign(id => {
   if (id === 'electron') return { app, dialog: { showErrorBox: (_title, error) => events.push(error) } };
   if (id === 'node:fs') return { promises: { readFile: async () => 'public-key' } };
   if (id === 'node:path') return path;
   if (id === './source-build') return buildModule.exports;
   if (id === './source-updates') return { SourceUpdates: class { constructor() { return updates; } } };
   if (id === './managed-context') return { setManagedContext: context => events.push(context.installersOnly?'installer-mode':'managed') };
   if (id === './source-profile') return { readProfile: () => null, localFeed: () => undefined };
   if (id === './installed-host') return { readInstalledHostSync: () => null };
   if (id === './installed-entry') return { startInstalled: async () => events.push('select-installed') };
   if (id === './index') { events.push('run'); return {}; }
   throw Error('Unexpected module ' + id);
  }, {main:asMain?module:{}})
 });
 if(direct)await module.exports.startManagedSource(env.SHUFFLE_SOURCE_ROOT,env.SHUFFLE_SOURCE_REVISION,env.SHUFFLE_SOURCE_NODE,true);
 await new Promise(resolve => setImmediate(resolve));
 return events;
}

test('installed Linux delegates staged code in its own Electron without recursively selecting or spawning',async()=>{
 assert.deepEqual(await launch({packaged:true,platform:'linux',env:sourceEnv,asMain:false}),[],'requiring the staged entry has no startup side effect');
 assert.deepEqual(await launch({packaged:true,platform:'linux',env:sourceEnv,asMain:false,direct:true}),[['attach',sourceEnv.SHUFFLE_SOURCE_REVISION],'installer-mode','run']);
});
const sourceEnv = { SHUFFLE_SOURCE_ROOT: path.resolve('profile/updates'), SHUFFLE_SOURCE_REVISION: 'a'.repeat(40), SHUFFLE_SOURCE_NODE: path.resolve('runtime/node.exe') };
test('packaged restarts select pending updates instead of attaching an inherited old revision', async () => {
 assert.deepEqual(await launch({ packaged: true, env: sourceEnv }), ['select-installed']);
 assert.deepEqual(await launch({ packaged: true }), ['select-installed']);
});
test('Linux packaged launches select installed source; macOS stays on installers', async () => {
 assert.deepEqual(await launch({ packaged: true, platform: 'linux' }), ['select-installed']);
 assert.deepEqual(await launch({ packaged: true, platform: 'linux', env: sourceEnv }), ['select-installed']);
 assert.deepEqual(await launch({ packaged: true, platform: 'darwin' }), ['run']);
 assert.deepEqual(await launch({ packaged: false, platform: 'linux', env: sourceEnv }), [['attach', sourceEnv.SHUFFLE_SOURCE_REVISION], 'managed', 'run']);
});
test('unpackaged source launches still attach their selected revision and development launches normally', async () => {
 assert.deepEqual(await launch({ packaged: false, env: sourceEnv }), [['attach', sourceEnv.SHUFFLE_SOURCE_REVISION], 'managed', 'run']);
 assert.deepEqual(await launch({ packaged: false }), ['run']);
});
