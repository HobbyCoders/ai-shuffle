import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { test } from 'node:test';

function fixture() {
 const calls = []; const timers = [];
 const app = { relaunch: options => calls.push(options), quit: () => calls.push('quit') };
 const module = { exports: {} };
 vm.runInNewContext(readFileSync(new URL('../dist/main/updater.js', import.meta.url), 'utf8'), {
  exports: module.exports, module, require: id => id === 'electron' ? { app } : path,
  process: { env: {} }, setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout() {},
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

test('shutdown failures retain staged updates for direct install retry without rebuilding', async () => {
 const { updater, calls } = fixture(); let builds = 0; let attempts = 0;
 updater.useApplications(source({ download: async () => { builds++; return ready; } }));
 updater.onBeforeInstall(async () => { if (++attempts === 1) throw Error('Database did not stop'); });
 await updater.check(); await updater.download(); await updater.install();
 assert.equal(updater.current.phase, 'ready'); assert.match(updater.current.error, /Database did not stop/); assert.equal(calls.length, 0);
 await updater.check(); await updater.download(); await updater.install();
 assert.equal(builds, 1); assert.equal(attempts, 2); assert.equal(calls.at(-1), 'quit'); assert.equal(updater.current.error, null);
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
