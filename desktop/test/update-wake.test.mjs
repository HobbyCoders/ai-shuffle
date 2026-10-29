import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { test } from 'node:test';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const timed = require('../dist/main/timed-phase.js');

function fixture(env = {}) {
  const timers = [], module = { exports: {} }; let watch, stopped = 0, now = 100_000;
  class Clock extends Date { static now() { return now; } }
  vm.runInNewContext(readFileSync(new URL('../dist/main/updater.js', import.meta.url), 'utf8'), {
    exports: module.exports, module, require: id => id === 'electron' ? { app: { getVersion: () => '1.1.0' } } : id === './timed-phase' ? timed : path,
    process: { env, platform: 'win32' }, Date: Clock, console,
    setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout() {},
    setInterval: fn => { timers.push(fn); return timers.length; }, clearInterval() {}
  });
  const updater = new module.exports.Updater(); let request = {}, downloads = 0, reports = 0;
  updater.download = async () => { downloads++; };
  updater.setBridge(async () => { reports++; const next = request; request = {}; return next; }, async () => true,
    wake => { watch = wake; return () => { stopped++; }; });
  updater.start();
  return { updater, timers, get watch() { return watch; }, get stopped() { return stopped; },
    get downloads() { return downloads; }, get reports() { return reports; },
    queue: value => { request = value; }, advance: value => { now += value; } };
}
const settle = () => new Promise(setImmediate);

test('a queued web action reaches exchange immediately, without ticking the 30-second clock', async () => {
  const f = fixture(); await settle(); const initial = f.reports;
  assert.equal(typeof f.watch, 'function', 'the authenticated wake subscription must be installed');
  f.queue({ action: 'download', expires_at: 110 }); f.watch(); f.watch();
  await settle(); await settle();
  assert.equal(f.downloads, 1); assert.ok(f.reports > initial);
  f.updater.stop(); assert.equal(f.stopped, 1);
});

test('late hints do not replay expired actions and stop removes the listener', async () => {
  const f = fixture(); await settle(); f.advance(20_000);
  f.queue({ action: 'download', expires_at: 110 }); f.watch(); await settle();
  assert.equal(f.downloads, 0); f.updater.stop();
  const reports = f.reports; f.watch(); await settle(); assert.equal(f.reports, reports);
  assert.equal(f.stopped, 1);
});

test('the push kill switch retains the heartbeat and needs no subscription', async () => {
  const f = fixture({ AI_SHUFFLE_PERF_DESKTOP_UPDATE_PUSH: '0' }); await settle();
  assert.equal(f.watch, undefined); assert.ok(f.timers.length > 0);
  f.updater.stop(); assert.equal(f.stopped, 0);
});
