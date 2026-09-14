import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { test } from 'node:test';

function fixture(env = {}) {
 const calls = []; const timers = [];
 const app = { relaunch: options => calls.push(options), quit: () => calls.push('quit') };
 const module = { exports: {} };
 vm.runInNewContext(readFileSync(new URL('../dist/main/updater.js', import.meta.url), 'utf8'), {
  exports: module.exports, module, require: id => id === 'electron' ? { app } : path,
  process: { env }, setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout() {},
  setInterval: fn => { timers.push(fn); return timers.length; }, clearInterval() {}, Date
 });
 return { updater: new module.exports.Updater(), calls, timers, app };
}
const available = { kind: 'available', version: 'bbbbbbbbbbbb' };
const ready = { ...available, kind: 'ready' };
function source(overrides = {}) { return {
 check: async () => available, download: async () => ready,
 version: 'a'.repeat(40), options: { root: 'C:/isolated/source' },
 application: async () => ({ node: 'C:/isolated/new-node/node.exe', desktopDir: 'C:/isolated/desktop' }), ...overrides
}; }
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; }

test('worker bridge performs one-shot downloads and refuses expired or busy restarts', async () => {
 const {updater,calls}=fixture();let request={};const seen=[];
 updater.useApplications(source());
 updater.setBridge(async state=>{seen.push(state.phase);const next=request;request={};return next;},async()=>false);
 await updater.check();updater.start();await new Promise(setImmediate);
 request={action:'download',expires_at:Date.now()/1000+10};await updater.exchange();await new Promise(setImmediate);
 assert.equal(updater.current.phase,'ready',JSON.stringify({seen,request,timer:updater.bridgeTimer,exchanging:updater.exchanging,current:updater.current}));
 request={action:'install',expires_at:Date.now()/1000+10};await updater.exchange();
 assert.match(updater.current.error,/Busy/);assert.equal(calls.length,0);
 updater.setBridge(async()=>({action:'install',expires_at:0}),async()=>true);await updater.exchange();assert.equal(calls.length,0);
 updater.stop();assert(seen.length>0);
});
test('account policy gates checks, downloads and staged installs, and failures fail closed', async () => {
 const { updater, calls } = fixture(); let enabled = false; let checks = 0;
 updater.setPolicy(async () => enabled);
 updater.useApplications(source({check:async()=>{ checks++; return available; }}));
 await updater.check(); assert.equal(checks,0);
 enabled=true; await updater.check(); assert.equal(checks,1);
 enabled=false; await updater.download(); assert.equal(updater.current.phase,'available');
 enabled=true; await updater.download(); assert.equal(updater.current.phase,'ready');
 enabled=false; await updater.install(); assert.equal(calls.length,0);
 updater.setPolicy(async()=>{throw Error('offline');});
 await updater.install(); assert.equal(calls.length,0); assert.match(updater.current.error,/Could not read/);
 updater.setPolicy(async()=>true); await updater.install(); assert.equal(calls.at(-1),'quit');
});
test('metadata checks expose available; only explicit download builds, then install stops services and relaunches', async () => {
 const { updater, calls } = fixture(); const states = []; updater.subscribe(s => states.push(s)); let downloads = 0;
 updater.useApplications(source({ download: async progress => { downloads++; progress(null, 'Building frontend…'); return ready; } }));
 updater.onBeforeInstall(async () => { calls.push('stop-worker'); calls.push('stop-postgres'); });
 await updater.install(); await updater.check(); assert.equal(updater.current.phase, 'available');
 assert.equal(downloads, 0); assert.equal(states.some(s => s.phase === 'downloading'), false);
 await updater.install(); assert.equal(calls.length, 0);
 await updater.download(); assert.equal(updater.current.phase, 'ready'); assert.equal(downloads, 1);
 assert(states.some(s => s.phase === 'downloading' && s.message === 'Building frontend…' && s.percent === null));
 await updater.check(); await updater.download(); assert.equal(downloads, 1);
 await updater.install();
 assert.equal(calls[0], 'stop-worker'); assert.equal(calls[1], 'stop-postgres');
 assert.equal(calls[2].execPath, 'C:/isolated/new-node/node.exe');
 assert.deepEqual(Array.from(calls[2].args), [path.join('C:/isolated/desktop', 'scripts/bootstrap.mjs'), '--root', 'C:/isolated/source']);
 assert.equal(calls[3], 'quit');
 await updater.install(); assert.equal(calls.length, 4, 'double-click does not restart twice');
});

test('build errors retain the available version and retry without another check', async () => {
 const { updater } = fixture(); let attempts = 0;
 updater.useApplications(source({ download: async progress => { progress(42, 'Downloading…'); if (++attempts === 1) throw Error('Build interrupted'); return ready; } }));
 await updater.check(); await updater.download(); assert.equal(updater.current.phase, 'error');
 assert.equal(updater.current.version, available.version); assert.equal(updater.current.percent, null);
 await updater.download(); assert.equal(updater.current.phase, 'ready'); assert.equal(updater.current.error, null); assert.equal(attempts, 2);
});

test('installed app updates restart the existing EXE without a console launcher or baseline source directory', async () => {
 const { updater, calls } = fixture();
 updater.useApplications(source({ application: async () => assert.fail('Installer baseline is not a source directory') }), 'C:/Programs/AI Shuffle/AI Shuffle.exe');
 updater.onBeforeInstall(async () => calls.push('stopped'));
 await updater.check(); await updater.download(); await updater.install();
 assert.equal(calls[0], 'stopped');
 assert.equal(calls[1].execPath, 'C:/Programs/AI Shuffle/AI Shuffle.exe');
 assert.deepEqual(Array.from(calls[1].args), []);
 assert.equal(calls[2], 'quit');
});

test('shutdown failures retain staged updates for direct install retry without rebuilding', async () => {
 const { updater, calls } = fixture(); let builds = 0; let attempts = 0;
 updater.useApplications(source({ download: async () => { builds++; return ready; } }));
 updater.onBeforeInstall(async () => { if (++attempts === 1) throw Error('Database did not stop'); });
 await updater.check(); await updater.download(); await updater.install();
 assert.equal(updater.current.phase, 'ready'); assert.match(updater.current.error, /Database did not stop/); assert.equal(calls.length, 0);
 await updater.check(); await updater.download(); await updater.install();
 assert.equal(builds, 1); assert.equal(attempts, 2); assert.equal(calls.at(-1), 'quit'); assert.equal(updater.current.error, null);
});

test('installed restart drops the running revision so legacy launchers select the pending update', async () => {
 const env = { SHUFFLE_SOURCE_ROOT: 'C:/isolated/source', SHUFFLE_SOURCE_REVISION: 'a'.repeat(40), SHUFFLE_SOURCE_NODE: 'C:/runtime/node.exe', PATH: 'keep-path' };
 const { updater, app } = fixture(env);
 let inherited;
 app.relaunch = () => { inherited = { ...env }; };
 updater.useApplications(source({ pendingVersion: 'b'.repeat(40) }), 'C:/Programs/AI Shuffle.exe');
 await updater.install();
 assert.deepEqual(inherited, { PATH: 'keep-path' });
 assert.deepEqual(env, { PATH: 'keep-path' });
});

test('failed installed relaunch restores source context and remains retryable', async () => {
 const original = { SHUFFLE_SOURCE_ROOT: 'C:/isolated/source', SHUFFLE_SOURCE_REVISION: 'a'.repeat(40), SHUFFLE_SOURCE_NODE: 'C:/runtime/node.exe' };
 const env = { ...original };
 const { updater, app } = fixture(env);
 app.relaunch = () => { assert.deepEqual(env, {}); throw Error('Relaunch failed'); };
 updater.useApplications(source({ pendingVersion: 'b'.repeat(40) }), 'C:/Programs/AI Shuffle.exe');
 await updater.install();
 assert.deepEqual(env, original);
 assert.equal(updater.current.phase, 'ready');
 assert.match(updater.current.error, /Relaunch failed/);
 app.relaunch = () => assert.deepEqual(env, {});
 await updater.install();
 assert.equal(updater.current.error, null);
});

test('relaunch failure is retryable without downloading again', async () => {
 const { updater, app, calls } = fixture(); const relaunch = app.relaunch;
 updater.useApplications(source({ pendingVersion: 'b'.repeat(40), download: async () => { throw Error('Must not download'); } }));
 app.relaunch = () => { throw Error('Relaunch failed'); };
 await updater.install(); assert.equal(updater.current.phase, 'ready'); assert.match(updater.current.error, /Relaunch failed/);
 app.relaunch = relaunch; await updater.install(); assert.equal(calls.at(-1), 'quit');
});

test('pending source state is immediately ready and checks and downloads leave it intact', async () => {
 const { updater } = fixture();
 updater.useApplications(source({ pendingVersion: 'b'.repeat(40), check: async () => { assert.fail('pending must not check'); }, download: async () => { assert.fail('pending must not rebuild'); } }));
 assert.equal(updater.current.phase, 'ready'); assert.equal(updater.current.version, ready.version);
 await updater.check(); await updater.download(); assert.equal(updater.current.phase, 'ready');
});

test('bridge reports the running revision separately from the staged update', async () => {
 const { updater } = fixture(); const seen = [];
 updater.useApplications(source({ pendingVersion: 'b'.repeat(40) }));
 updater.setBridge(async state => { seen.push(state); return {}; }, async () => true);
 await updater.exchange();
 assert.equal(seen[0].version, 'b'.repeat(12));
 assert.equal(seen[0].running_version, 'a'.repeat(40));
 updater.useApplications(source({ version: 'b'.repeat(40), pendingVersion: 'b'.repeat(40) }));
 await updater.exchange();
 assert.equal(seen[1].phase, 'idle');
 assert.equal(seen[1].running_version, 'b'.repeat(40));
});

test('a pending revision already running its first boot is not offered for installation again', async () => {
 const { updater, calls } = fixture();
 updater.useApplications(source({ pendingVersion: 'a'.repeat(40) }));
 assert.equal(updater.current.phase, 'idle'); assert.equal(updater.current.version, null);
 await updater.install(); assert.deepEqual(calls, []);
});

test('overlapping check, download and install commands never race or duplicate work', async () => {
 const { updater, calls } = fixture(); const checking = deferred(); const downloading = deferred(); const stopping = deferred();
 let checks = 0; let downloads = 0; let stops = 0;
 updater.useApplications(source({ check: async () => { checks++; await checking.promise; return available; }, download: async () => { downloads++; await downloading.promise; return ready; } }));
 updater.onBeforeInstall(async () => { stops++; await stopping.promise; });
 const check = updater.check(); await updater.check(); await updater.download(); await updater.install();
 assert.equal(checks, 1); assert.equal(downloads, 0); assert.equal(stops, 0);
 checking.resolve(); await check;
 const download = updater.download(); await updater.check(); await updater.download(); await updater.install();
 assert.equal(checks, 1); assert.equal(downloads, 1); assert.equal(stops, 0);
 downloading.resolve(); await download;
 const install = updater.install(); await updater.check(); await updater.download(); await updater.install();
 assert.equal(checks, 1); assert.equal(downloads, 1); assert.equal(stops, 1);
 stopping.resolve(); await install; assert.equal(calls.length, 2);
});

test('initial and periodic checks are metadata-only and never enable interactive authentication', async () => {
 const { updater, timers } = fixture(); const modes = [];
 updater.useApplications(source({ check: async (progress, interactive) => { assert.equal(progress, undefined); modes.push(interactive); return available; }, download: async () => { assert.fail('automatic download'); } }));
 updater.start(); assert.equal(timers.length, 2);
 for (const timer of timers) { timer(); await new Promise(resolve => setImmediate(resolve)); }
 assert.deepEqual(modes, [false, false]); assert.equal(updater.current.phase, 'available');
 await updater.check(true); assert.deepEqual(modes, [false, false, true]); updater.stop();
});

test('metadata failures retry and a current result clears stale availability', async () => {
 const { updater } = fixture(); let attempts = 0;
 updater.useApplications(source({ check: async () => { if (++attempts === 1) throw Error('Offline'); return attempts === 2 ? available : { kind: 'current' }; } }));
 await updater.check(); assert.equal(updater.current.phase, 'error');
 await updater.check(); assert.equal(updater.current.phase, 'available'); assert.equal(updater.current.error, null);
 await updater.check(); assert.equal(updater.current.phase, 'idle'); assert.equal(updater.current.version, null);
});
