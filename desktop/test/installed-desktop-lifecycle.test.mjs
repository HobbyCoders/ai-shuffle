import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { closeInstalledDesktop, debuggerPorts, describeSnapshot, desktopProcesses, desktopSnapshot, parseTasklist, waitForDesktopExit } from '../scripts/installed-desktop-lifecycle.mjs';
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

test('tasklist rows give the image and pid; its no-match notice gives none', () => {
 assert.deepEqual(parseTasklist('INFO: No tasks are running which match the specified criteria.\r\n'), []);
 assert.deepEqual(parseTasklist('"electron.exe","6532","Console","1","120,404 K"\r\n"electron.exe","7012","Console","1","40,100 K"\r\n'),
  [{ image: 'electron.exe', pid: 6532 }, { image: 'electron.exe', pid: 7012 }]);
});

test('the next start waits for the updated app (electron.exe), not only for the AI Shuffle.exe launcher', async () => {
 // The fixed wait this replaces looked for AI Shuffle.exe only. The launcher
 // has exited by then; the updated app still holds the single-instance lock.
 const running = { 'AI Shuffle.exe': [], 'electron.exe': ['"electron.exe","6532","Console","1","120,404 K"'] };
 const list = () => desktopProcesses(image => running[image].join('\r\n') || 'INFO: No tasks are running which match the specified criteria.', () => []);
 let done = false;
 const waiting = waitForDesktopExit(5000, list, 1).then(() => { done = true; });
 await new Promise(resolve => setTimeout(resolve, 20));
 assert.equal(done, false);
 running['electron.exe'] = [];
 await waiting;
 assert.equal(done, true);
});

test('the exit wait excludes only the splash entry and its Chromium descendants', async () => {
 const list = image => image === 'electron.exe' ? [11, 12, 13, 14, 15].map(pid => `"electron.exe","${pid}"`).join('\n') : '';
 const details = () => [
  { ProcessId: 11, ParentProcessId: 1, CommandLine: 'electron.exe "C:\\source\\desktop\\dist\\main\\update-splash-main.js" --root=C:\\updates' },
  { ProcessId: 12, ParentProcessId: 11, CommandLine: 'electron.exe --type=renderer' },
  { ProcessId: 13, ParentProcessId: 12, CommandLine: 'electron.exe --type=utility' },
  { ProcessId: 14, ParentProcessId: 1, CommandLine: 'electron.exe C:/source/desktop/dist/main/managed-entry.js' },
  { ProcessId: 15, ParentProcessId: 1, CommandLine: null },
 ];
 assert.deepEqual(desktopProcesses(list, details).map(p => p.pid), [14, 15]);
 assert.equal(desktopProcesses(list, () => { throw Error('access denied'); }).length, 5, 'query failure stays conservative');
 await waitForDesktopExit(0, () => desktopProcesses(image => image === 'electron.exe' ? '"electron.exe","11"\n"electron.exe","12"' : '', details));
});

test('a desktop that never exits names what is still running', async () => {
 await assert.rejects(waitForDesktopExit(0, () => [{ image: 'electron.exe', pid: 6532 }], 1), /still running .*electron\.exe 6532.*single-instance lock/);
});

test("the updated app's own debugger port wins over a DevToolsActivePort the launcher left behind", () => {
 // Run 38983878672: the app's write failed with a sharing violation, so the file kept the launcher's port 57484.
 const launch = '\r\nDevTools listening on ws://127.0.0.1:57520/devtools/browser/3e93bb74\r\n[2516:ERROR:devtools_http_handler.cc:305] Error writing DevTools active port to file\r\n';
 assert.deepEqual(debuggerPorts(launch, '57484\n/devtools/browser/56969aac'), ['57520', '57484']);
 assert.deepEqual(debuggerPorts('DevTools listening on ws://127.0.0.1:1111/a\nDevTools listening on ws://127.0.0.1:2222/b\n', null), ['2222', '1111']);
 // The older installer runs its app in the launcher itself: only the file names a port.
 assert.deepEqual(debuggerPorts('', '59010\n/devtools'), ['59010']);
 assert.deepEqual(debuggerPorts('', 'garbage'), []);
});

test('a snapshot failure is recorded, never thrown', () => {
 const failed = desktopSnapshot(['C:\\install'], () => { throw Error('powershell missing'); });
 assert.match(failed.error, /powershell missing/);
 assert.match(describeSnapshot(failed), /snapshot failed/);
 const one = desktopSnapshot(['C:\\install'], () => JSON.stringify({ processes: { pid: 5, parent: 4, name: 'electron.exe', started: 't', command: 'electron.exe app' }, windows: '5 visible Chrome_WidgetWin_1 \'AI Shuffle\'' }));
 assert.equal(one.processes.length, 1);
 assert.match(describeSnapshot(one), /electron\.exe 5 \(parent 4.*\n.*window 5 visible/);
 assert.equal(describeSnapshot({ processes: [], windows: [] }), '  no AI Shuffle process is running');
});
