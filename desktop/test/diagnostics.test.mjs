import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
const require = createRequire(import.meta.url);
const { desktopSampler } = require('../dist/main/diagnostics.js');

test('diagnostics IPC rejects foreign windows, subframes and origins before sampling', () => {
  const handlers = new Map();
  let calls = 0;
  const exports = {};
  const source = readFileSync(new URL('../dist/main/ipc.js', import.meta.url), 'utf8');
  runInNewContext(source, {exports, URL, process, require: name => {
    if (name === 'electron') return {ipcMain: {handle: (key, fn) => handlers.set(key, fn), on() {}},
      app: {getAppMetrics() { calls++; return []; }}};
    if (name === './managed-context') return {managedContext: () => null};
    if (name === './diagnostics') return {desktopSampler};
    if (name === './listener') return require('../dist/main/listener.js');
    if (name === './disclosure') return require('../dist/main/disclosure.js');
    throw new Error(name);
  }});
  exports.registerIpc({window: {isOwnSender: id => id === 1, currentWorkerUrl: 'http://127.0.0.1:18340'}, updater: {}, worker: {}});
  const invoke = handlers.get('desktop:diagnostics');
  const frame = {url: 'http://127.0.0.1:18340/'};
  const sender = {id: 1, mainFrame: frame};
  assert.throws(() => invoke({sender: {...sender, id: 2}, senderFrame: frame}), /not allowed/);
  assert.throws(() => invoke({sender, senderFrame: {...frame}}), /not allowed/);
  frame.url = 'https://unrelated.example/';
  assert.throws(() => invoke({sender, senderFrame: frame}), /not allowed/);
  assert.equal(calls, 0);
  frame.url = 'http://127.0.0.1:18340/?session=private';
  assert.equal(invoke({sender, senderFrame: frame}).scope, 'electron_only');
  assert.equal(calls, 1);
});


test('desktop samples are opt-in, bounded, cached and forget exited/reused identities', () => {
  let now = 0, calls = 0, ticks = 2, creationTime = 1000;
  const sample = desktopSampler(() => {
    calls++;
    return Array.from({length: 257}, (_, pid) => ({pid, creationTime, type: 'GPU',
      cpu: { cumulativeCPUUsage: ticks }, memory: { workingSetSize: 2048, privateBytes: 1024 },
      serviceName: 'never expose this', name: 'never expose this'}));
  }, () => now, 4);
  assert.equal(calls, 0);
  const first = sample();
  assert.equal(first.processes.length, 256);
  assert.equal(first.omitted_processes, 1);
  assert.equal(first.processes[0].cpu_pct_machine, null);
  assert.equal(first.processes[0].working_set_mib, 2);
  assert.equal(first.processes[0].electron_private_mib, 1);
  assert.equal(first.processes[0].started_at, 1);
  assert.equal(JSON.stringify(first).includes('never expose'), false);
  assert.equal(sample(), first);
  assert.equal(calls, 1);
  now = 5000; ticks = 3;
  assert.equal(sample().processes[0].cpu_pct_machine, 5);
  now = 10000; creationTime = 2000;
  assert.equal(sample().processes[0].cpu_pct_machine, null);
});
