import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const code = readFileSync(new URL('../dist/main/managed-entry.js', import.meta.url), 'utf8');
async function launch({ packaged, env = {}, platform = 'win32', asMain = true, direct = false, host = null, confirm = () => 'none' }) {
 const events = [];
 const module = { exports: {} };
 const app = { isPackaged: packaged, whenReady: async () => {}, quit: () => events.push('quit'), setName: () => {}, setPath: () => {} };
 const updates = { attach: async revision => events.push(['attach', revision]), application: async () => ({node:path.resolve('runtime/node/bin/node'),python:path.resolve('runtime/python/bin/python3')}) };
 const buildModule={exports:{}};vm.runInNewContext(readFileSync(new URL('../dist/main/source-build.js',import.meta.url),'utf8'),{module:buildModule,exports:buildModule.exports,require:createRequire(import.meta.url),process:{env}});
 vm.runInNewContext(code, {
  module, exports: module.exports, console: { log: () => {}, error: (...args) => events.push(['error', String(args.at(-1))]) }, __dirname: path.resolve('desktop/dist/main'), process: { platform, env },
  require: Object.assign(id => {
   if (id === 'electron') return { app, dialog: { showErrorBox: (_title, error) => events.push(error) } };
   if (id === 'node:fs') return { promises: { readFile: async () => 'public-key' } };
   if (id === 'node:path') return path;
   if (id === './source-build') return buildModule.exports;
   if (id === './source-updates') return { SourceUpdates: class { constructor() { return updates; } } };
   if (id === './managed-context') return { setManagedContext: context => events.push(context.installersOnly?'installer-mode':'managed') };
   if (id === './source-profile') return { readProfile: () => null, localFeed: () => undefined };
   if (id === './installed-host') return { readInstalledHostSync: () => host, launcherVersion: value => value === undefined ? 0 : Number(value), LAUNCHER_VERSION_ENV: 'SHUFFLE_LAUNCHER_VERSION' };
   if (id === './launcher-swap') return { confirmLauncher: (...args) => { events.push(['confirm', ...args]); return confirm(); } };
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
 assert.deepEqual(await launch({packaged:true,platform:'linux',env:sourceEnv,asMain:false,direct:true,host:{executable:path.resolve('AI Shuffle.AppImage'),name:'AI Shuffle',userData:path.resolve('profile')},confirm:()=>{throw Error('Linux must not settle Windows launcher swaps');}}),[['attach',sourceEnv.SHUFFLE_SOURCE_REVISION],'installer-mode','run']);
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
test('#1821 D5: an installed source start settles a launcher swap right after attach, with the launcher version it was given', async () => {
 const host = { executable: path.resolve('Programs/AI Shuffle.exe'), name: 'AI Shuffle', userData: path.resolve('profile') };
 const events = await launch({ packaged: false, env: { ...sourceEnv, SHUFFLE_LAUNCHER_VERSION: '2' }, host });
 assert.deepEqual(events, [['attach', sourceEnv.SHUFFLE_SOURCE_REVISION], ['confirm', sourceEnv.SHUFFLE_SOURCE_ROOT, host.executable, 2, sourceEnv.SHUFFLE_SOURCE_REVISION], 'managed', 'run']);
 // A broken record never stops the app.
 const failed = await launch({ packaged: false, env: sourceEnv, host, confirm: () => { throw Error('Invalid launcher swap record'); } });
 assert.deepEqual(failed.slice(-3), [['error', 'Error: Invalid launcher swap record'], 'managed', 'run']);
 // Profiles (no installed host) never touch it.
 assert(!(await launch({ packaged: false, env: sourceEnv })).some(event => event[0] === 'confirm'));
});
