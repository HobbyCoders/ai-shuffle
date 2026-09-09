import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { test } from 'node:test';

const code = readFileSync(new URL('../src/pages/worker.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../src/pages/worker.html', import.meta.url), 'utf8');
const tick = () => new Promise((resolve) => setImmediate(resolve));

function page({ state = { phase: 'starting', postgres: { phase: 'starting' } }, getState, restart, search = '' } = {}) {
 const elements = new Map([...html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)].map(([tag, id]) => [id, {
  hidden: /\bhidden\b/.test(tag), disabled: false, open: false, textContent: '', dataset: {},
  scrollTop: 0, clientHeight: 100, scrollHeight: 100, listeners: {},
  addEventListener(name, listener) { this.listeners[name] = listener; }
 }]));
 const timers = new Map(); let nextTimer = 0; let onState; let onLog;
 const calls = { tail: 0, restart: 0, openLogs: 0 };
 const bridge = {
  getState: getState || (() => Promise.resolve(state)),
  onState: (callback) => { onState = callback; }, onLog: (callback) => { onLog = callback; },
  tail: async () => { calls.tail++; return ['test diagnostic entry']; },
  restart: async () => { calls.restart++; if (restart) await restart(); },
  openLogs: () => { calls.openLogs++; }
 };
 vm.runInNewContext(code, {
  window: { location: { search }, shuffleDesktop: { version: 'test' }, shuffleDesktopWorker: bridge },
  document: { getElementById: (id) => { assert.ok(elements.has(id), `unknown element ${id}`); return elements.get(id); } },
  URLSearchParams, Error,
  setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; },
  clearTimeout: (id) => timers.delete(id)
 });
 return { el: (id) => elements.get(id), calls, timers, emit: (state) => onState(state), log: () => onLog(), bridge };
}

test('ordinary startup is quiet, reflects readiness, and loads logs only when requested', async () => {
 const p = page(); await tick();
 assert.equal(p.el('status').dataset.mode, 'starting');
 assert.equal(p.el('status-detail').textContent, 'Preparing your local database.');
 assert.equal(p.el('status-error').hidden, true);
 assert.equal(p.el('restart').hidden, true);
 assert.equal(p.el('diagnostics').open, false);
 p.log(); await tick(); assert.equal(p.calls.tail, 0);
 p.el('diagnostics').open = true; p.el('diagnostics').listeners.toggle(); await tick();
 assert.equal(p.calls.tail, 1); assert.equal(p.el('log').textContent, 'test diagnostic entry');
 p.el('open-logs').listeners.click(); assert.equal(p.calls.openLogs, 1);
 p.emit({ phase: 'ready', postgres: { phase: 'ready' }, url: 'http://127.0.0.1:8000' });
 assert.equal(p.el('status').dataset.mode, 'ready');
 assert.equal(p.el('status-detail').textContent, 'Your local worker is running.');
 assert.equal(p.timers.size, 0);
});

test('credential protection failure surfaces exact error and can retry without stale failure', async () => {
 const failed = { phase: 'idle', postgres: { phase: 'failed', error: 'Could not protect the local credential directory.' } };
 const p = page({ state: failed }); await tick();
 assert.equal(p.el('status').dataset.mode, 'failed');
 assert.equal(p.el('status-heading').textContent, 'Your local database could not start');
 assert.equal(p.el('status-error').textContent, failed.postgres.error);
 assert.equal(p.el('status-error').hidden, false); assert.equal(p.el('restart').hidden, false);
 assert.equal(p.timers.size, 0);
 p.bridge.getState = async () => ({ phase: 'starting', postgres: { phase: 'ready' } });
 await p.el('restart').listeners.click();
 assert.equal(p.calls.restart, 1); assert.equal(p.el('status-error').hidden, true);
 assert.equal(p.el('status').dataset.mode, 'starting'); assert.equal(p.el('restart').disabled, false);
 p.emit({ phase: 'ready', postgres: { phase: 'ready' } });
 assert.equal(p.el('status').dataset.mode, 'ready');
});

test('retry rejection provides actionable visible error and permits another attempt', async () => {
 const p = page({ state: { phase: 'stopped' }, restart: () => { throw new Error('Restart service unavailable'); } }); await tick();
 await p.el('restart').listeners.click();
 assert.equal(p.el('status').dataset.mode, 'failed');
 assert.equal(p.el('status-error').textContent, 'Restart service unavailable');
 assert.equal(p.el('restart').hidden, false); assert.equal(p.el('restart').disabled, false);
 await p.el('restart').listeners.click(); assert.equal(p.calls.restart, 2);
});

test('navigation failure persists through status events and is cleared only by retry', async () => {
 const p = page({ search: '?error=Failed%20to%20load%20app', state: { phase: 'ready' } }); await tick();
 assert.equal(p.el('status-error').textContent, 'Failed to load app');
 p.emit({ phase: 'ready' }); assert.equal(p.el('status').dataset.mode, 'failed');
 await p.el('restart').listeners.click(); assert.equal(p.el('status-error').hidden, true);
 assert.equal(p.el('status').dataset.mode, 'ready');
});

test('slow startup stops decorative animation without declaring readiness or losing later failure', async () => {
 const p = page(); await tick();
 const timer = [...p.timers.values()][0]; assert.equal(timer.delay, 60_000); timer.callback();
 assert.equal(p.el('status').dataset.mode, 'slow');
 assert.match(p.el('status-detail').textContent, /still preparing/);
 assert.equal(p.el('restart').hidden, true);
 p.emit({ phase: 'failed', postgres: { phase: 'ready' }, error: 'Readiness deadline exceeded' });
 assert.equal(p.el('status').dataset.mode, 'failed');
 assert.equal(p.el('status-error').textContent, 'Readiness deadline exceeded');
});

test('state event during initial snapshot wins over stale snapshot response', async () => {
 let resolve;
 const p = page({ getState: () => new Promise((done) => { resolve = done; }) });
 p.emit({ phase: 'failed', error: 'Local startup failed' });
 resolve({ phase: 'starting' }); await tick();
 assert.equal(p.el('status-error').textContent, 'Local startup failed');
 assert.equal(p.el('status').dataset.mode, 'failed');
});

test('unavailable status bridge is a visible error rather than an endless splash', async () => {
 const p = page({ getState: () => Promise.reject(new Error('IPC unavailable')) }); await tick();
 assert.equal(p.el('status').dataset.mode, 'failed');
 assert.equal(p.el('status-error').textContent, 'IPC unavailable');
 assert.equal(p.el('restart').hidden, false);
});
