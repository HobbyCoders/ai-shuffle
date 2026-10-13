import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { closeInstalledDesktop } from '../scripts/installed-desktop-lifecycle.mjs';
const { QuitGuard } = createRequire(import.meta.url)('../dist/main/quit.js');
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const tick = () => new Promise(resolve => setImmediate(resolve));
function debuggerClient() {
 const browser = new EventEmitter();
 let connected = true;
 browser.closes = 0;
 browser.isConnected = () => connected;
 browser.disconnect = () => { connected = false; browser.emit('disconnected'); };
 browser.close = async () => { browser.closes++; browser.disconnect(); };
 return browser;
}

test('worker stop does not finish close while QuitGuard is still shutting down PostgreSQL', async () => {
 const browser = debuggerClient(), workerStopped = deferred(), postgresStopped = deferred();
 let quitting;
 const guard = new QuitGuard({
  activeSessionCount: async () => 0, confirm: async () => true,
  stop: async () => { workerStopped.resolve(); await postgresStopped.promise; },
  quit: () => browser.disconnect(),
  failed: error => { throw error; }
 });
 const page = { isClosed: () => false, evaluate: async () => { quitting = guard.request(); } };
 let finished = false;
 const closing = closeInstalledDesktop(page, browser, () => workerStopped.promise).then(() => { finished = true; });
 await workerStopped.promise;
 await tick();
 assert.equal(guard.canQuit, false);
 assert.equal(finished, false, 'must not start the next launch against the old window/lock');
 assert.equal(browser.closes, 0, 'client-side disconnect is not proof that Electron exited');
 postgresStopped.resolve();
 await quitting;
 await closing;
 assert.equal(guard.canQuit, true);
 assert.equal(browser.closes, 1);
 assert.equal(browser.listenerCount('disconnected'), 0);
});

test('subscribes before requesting close so an immediate app exit is not missed', async () => {
 const browser = debuggerClient();
 const page = { isClosed: () => false, evaluate: async () => { browser.disconnect(); throw Error('Target closed'); } };
 await closeInstalledDesktop(page, browser, async () => {}, 0);
 assert.equal(browser.closes, 1);
});

test('a worker that stopped without Electron exiting fails instead of detaching and relaunching', async () => {
 const browser = debuggerClient();
 const page = { isClosed: () => false, evaluate: async () => {} };
 await assert.rejects(closeInstalledDesktop(page, browser, async () => {}, 0), /Installed Electron did not exit/);
 assert.equal(browser.closes, 0);
 assert.equal(browser.listenerCount('disconnected'), 0);
});

test('cleanup tolerates an already disconnected app, but still waits for its worker', async () => {
 const browser = debuggerClient(); browser.disconnect();
 let waited = false;
 await closeInstalledDesktop({ isClosed: () => true }, browser, async () => { waited = true; }, 0);
 assert.equal(waited, true);
 assert.equal(browser.listenerCount('disconnected'), 0);
});
