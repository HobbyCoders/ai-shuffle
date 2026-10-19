import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import vm from 'node:vm';
import { test } from 'node:test';
const require = createRequire(import.meta.url);
const helpers = require('../dist/main/update-splash.js');
const { splashPhase, launchUpdateSplash, imageRunning, launcherGaveUp, launcherImage, SPLASH_LIMIT_MS } = helpers;
const A = 'a'.repeat(40), B = 'b'.repeat(40);

test('the window follows checking, starting, attachment and launcher failure', () => {
  assert.equal(splashPhase(null, B), 'checking');
  assert.equal(splashPhase({ active: A, pending: B }, B), 'checking');
  assert.equal(splashPhase({ active: A, pending: B, booting: B }, B), 'starting');
  assert.equal(splashPhase({ active: A, pending: B, booting: B, waiting: B }, B), 'attached');
  assert.equal(splashPhase({ booting: B, mayHaveMigrated: { [B]: 2 } }, B), 'attached');
  assert.equal(splashPhase({ active: B }, B), 'attached');
  for (const state of [{ active: A, failed: [B] }, { active: A, staged: B }, { active: A, pending: A }, { active: A, blocked: true }]) assert.equal(splashPhase(state, B), 'stopped');
});

test('the detached process has a separate profile and can be closed when relaunch fails', () => {
  const runs = [];
  const child = new EventEmitter();
  child.unref = () => runs.push('unref');
  child.kill = () => runs.push('kill');
  const options = { electron: 'electron.exe', mainDir: 'dist/main', root: 'updates', revision: B, release: '1.0.2', profile: 'splash-profile', launcher: 'AI Shuffle.exe' };
  const close = launchUpdateSplash(options, (file, args, spawnOptions) => { runs.push({ file, args, spawnOptions }); return child; });
  const call = runs[0];
  assert.equal(call.file, options.electron);
  assert.equal(call.args[0], path.join('dist/main', 'update-splash-main.js'));
  assert.ok(call.args.includes('--profile=splash-profile'));
  assert.ok(call.args.includes('--launcher=AI Shuffle.exe'));
  assert.equal(call.spawnOptions.detached, true);
  assert.equal(call.spawnOptions.stdio, 'ignore');
  for (const key of ['ELECTRON_RUN_AS_NODE', 'SHUFFLE_SOURCE_ROOT', 'SHUFFLE_SOURCE_REVISION', 'SHUFFLE_SOURCE_NODE']) assert.equal(call.spawnOptions.env[key], undefined);
  assert.equal(runs.at(-1), 'unref');
  close(); assert.equal(runs.at(-1), 'kill');
  assert.equal(launchUpdateSplash(options, () => { throw Error('spawn failed'); }), null);
  child.emit('error', Error('async spawn failed')); // cannot become an unhandled error
});

test('a launcher that disappears during checking closes the notice after a bounded grace', async () => {
  assert.equal(launcherImage('C:\\Program Files\\AI Shuffle\\AI Shuffle.exe'), 'AI Shuffle.exe');
  assert.equal(launcherImage('bad".exe'), null);
  assert.equal(launcherGaveUp('checking', 0, null, 19_000), false);
  assert.equal(launcherGaveUp('checking', 0, null, 21_000), true);
  assert.equal(launcherGaveUp('checking', 0, 25_000, 30_000), false);
  assert.equal(launcherGaveUp('checking', 0, 25_000, 36_000), true);
  assert.equal(launcherGaveUp('starting', 0, null, 36_000), false);
  assert.equal(await imageRunning('AI Shuffle.exe', (_file, args, opts, cb) => { assert.equal(opts.timeout, 5000); cb(null, '"AI Shuffle.exe","123"'); }), true);
  assert.equal(await imageRunning('AI Shuffle.exe', (_file, _args, _opts, cb) => cb(null, 'INFO: No tasks are running')), false);
  assert.equal(await imageRunning('AI Shuffle.exe', (_file, _args, _opts, cb) => cb(Error('query failed'), '')), true);
});

/** Run the compiled entry, including its polling/quit loop, with Electron and clock fakes. */
async function entry(phases, { running = true, stalled = false } = {}) {
  let now = 0, quit;
  const ended = new Promise(resolve => { quit = resolve; });
  const events = [], timers = [];
  const app = { setName() {}, setPath(key, value) { events.push([key, value]); }, whenReady: async () => {}, quit, exit: quit, on() {} };
  class Window {
    constructor(options) { events.push(options); this.webContents = { setWindowOpenHandler() {}, on() {}, executeJavaScript: async code => events.push(code) }; }
    setMenu() {} async loadURL(url) { events.push(url); }
    isDestroyed() { return false; } isVisible() { return false; } show() { events.push('show'); }
  }
  let reads = 0;
  const file = new URL('../dist/main/update-splash-main.js', import.meta.url);
  vm.runInNewContext(readFileSync(file, 'utf8'), {
    exports: {}, process: { argv: ['electron', 'entry', `--root=${path.resolve('fixture')}`, `--profile=${path.resolve('splash-profile')}`, `--revision=${B}`, '--release=1.0.2', '--launcher=AI Shuffle.exe'] },
    Date: { now: () => now },
    setTimeout: (fn, ms) => { if (ms === SPLASH_LIMIT_MS) timers.push(fn); else { now += ms; Promise.resolve().then(fn); } return { unref() {} }; },
    require: id => id === 'electron' ? { app, BrowserWindow: Window } : id === 'node:fs' ? { promises: { readFile: async () => { reads++; if (stalled) return new Promise(() => {}); return '{}'; } } } : id === './source-updates' ? { parseSourceState: () => ({}) } : id === './update-splash' ? { ...helpers, splashPhase: () => phases[Math.min(reads - 1, phases.length - 1)], imageRunning: async () => running } : require(id),
  });
  if (stalled) { await new Promise(resolve => setImmediate(resolve)); timers[0](); }
  await ended;
  return { events, now, timers };
}
for (const terminal of ['attached', 'stopped']) test(`compiled window closes when ${terminal}`, async () => {
  const { events, timers } = await entry(['checking', 'starting', terminal]);
  assert.ok(events.includes('show'));
  assert.ok(events.some(event => typeof event === 'string' && event.includes('Starting the update to AI Shuffle 1.0.2')));
  const options = events.find(event => event?.webPreferences);
  assert.equal(options.webPreferences.sandbox, true);
  assert.equal(options.webPreferences.nodeIntegration, false);
  assert.equal(timers.length, 1);
});
test('compiled window closes if the launcher exits during its check', async () => {
  const { now } = await entry(['checking'], { running: false });
  assert.ok(now > 20_000 && now < 25_000);
});
test('watchdog closes even if reading the state never completes', async () => {
  await entry(['checking'], { stalled: true });
});
