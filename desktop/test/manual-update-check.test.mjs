import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { ManualUpdateCheck } from '../dist/main/manual-update-check.js';

function fixture(state = {}, response = 1) {
  const calls = [], dialogs = [];
  const updater = {
    current: { phase: 'idle', checkedAt: '2026-09-30T00:00:00Z', version: null, error: null, ...state },
    async check(interactive) { calls.push(['check', interactive]); },
    async download() { calls.push(['download']); },
    async install() { calls.push(['install']); },
  };
  const manual = new ManualUpdateCheck(updater, async options => { dialogs.push(options); return { response }; });
  return { manual, updater, calls, dialogs };
}
for (const state of [
  { phase: 'idle', message: null },
  { phase: 'error', error: 'Network refused' },
  { phase: 'idle', message: 'Updates are disabled' },
  { phase: 'idle', checkedAt: null },
  { phase: 'checking' }, { phase: 'downloading' }, { phase: 'unsupported' },
]) {
  test(`manual check reports ${JSON.stringify(state)} without starting an update`, async () => {
    const { manual, calls, dialogs } = fixture(state);
    await manual.run();
    assert.deepEqual(calls, [['check', true]]);
    assert.equal(dialogs.length, 1);
    assert.deepEqual(dialogs[0].buttons, ['OK']);
    assert.equal(/up to date/.test(dialogs[0].message), state.phase === 'idle' && state.message === null);
    if (state.error) assert.equal(dialogs[0].detail, state.error);
    if (state.message) assert.equal(dialogs[0].detail, state.message);
  });
}
for (const [phase, action] of [['available', 'download'], ['ready', 'install']]) {
  for (const response of [0, 1]) {
    test(`${phase} requires explicit confirmation before ${action}: response ${response}`, async () => {
      const { manual, calls, dialogs } = fixture({ phase, version: 'abc', release: '0.8.22' }, response);
      await manual.run();
      assert.deepEqual(calls, response === 0 ? [['check', true], [action]] : [['check', true]]);
      assert.match(dialogs[0].message + dialogs[0].detail, /0.8.22/);
      assert.equal(dialogs[0].cancelId, 1);
      assert.equal(dialogs[0].defaultId, 1);
      assert.equal(manual.active, false);
    });
  }
}
test('repeated tray clicks share one check and dialog; installer offers see the manual guard', async () => {
  const { updater, calls } = fixture({ phase: 'available', version: 'abc' });
  let resolve, dialogs = 0;
  const pending = new Promise(r => { resolve = r; });
  const manual = new ManualUpdateCheck(updater, async () => { dialogs++; assert.equal(manual.active, true); return pending; });
  const first = manual.run();
  await new Promise(setImmediate);
  await manual.run();
  resolve({ response: 1 }); await first;
  assert.equal(dialogs, 1); assert.deepEqual(calls, [['check', true]]);
});
test('a phase changed during the prompt cannot download or restart unexpectedly', async () => {
  const { updater, calls } = fixture({ phase: 'available', version: 'abc' });
  const manual = new ManualUpdateCheck(updater, async () => { updater.current.phase = 'downloading'; return { response: 0 }; });
  await manual.run(); assert.deepEqual(calls, [['check', true]]);
});
test('dialog failure releases the manual guard', async () => {
  const { updater } = fixture();
  const manual = new ManualUpdateCheck(updater, async () => { throw Error('dialog closed'); });
  await assert.rejects(manual.run(), /dialog closed/); assert.equal(manual.active, false);
});
test('tray and menu share the native result path and force reload has a shortcut', () => {
  const main = readFileSync(new URL('../src/main/index.ts', import.meta.url), 'utf8');
  assert.match(main, /checkForUpdates:.*[\s\S]*?manualUpdateCheck\.run\(\)/);
  assert.match(main, /if \(manualUpdateCheck\.active \|\| state.phase !== 'available'/);
  const menu = readFileSync(new URL('../src/main/menu.ts', import.meta.url), 'utf8');
  assert.match(menu, /role: 'forceReload', accelerator: 'CmdOrCtrl\+Shift\+R'/);
});

// The worker's plan, as app/core/update_workers.py reports it.
function plan(action, extra = {}) {
  return { action, ready: action === 'install', preparing: false, available: action === 'download', staged: 0,
           included: 2, held: '', waiting: false, ...extra };
}
function snap(planValue, workers) {
  return { allow_updates: true, update_all_workers: true, plan: planValue, workers: workers ?? [
    { device_id: 'desk', device_name: 'Desktop', role: planValue.action === 'install' ? 'staged' : 'available', label: 'Staged', release: '0.8.23' },
    { device_id: 'serverworker', device_name: 'Unraid', role: 'available', label: 'Not checked yet; will check and download' },
  ] };
}
function fleetFixture(snapshots, responses = [0], receipts = {}) {
  const calls = [], dialogs = [];
  const updater = {
    current: { phase: 'idle', checkedAt: null, version: null, error: null },
    async check() { calls.push(['local-check']); },
    async download() { calls.push(['local-download']); },
    async install() { calls.push(['local-install']); },
    async exchange() { calls.push(['exchange']); },
  };
  const fleet = {
    async workers() { const next = snapshots.length > 1 ? snapshots.shift() : snapshots[0]; if (next instanceof Error) throw next; return next; },
    async act(action) {
      calls.push(['act', action]);
      const receipt = receipts[action];
      if (receipt instanceof Error) throw receipt;
      return receipt ?? { sent: action, results: [{ device_name: 'Desktop', accepted: true, reason: 'Request queued' }] };
    },
  };
  const manual = new ManualUpdateCheck(updater, async options => { dialogs.push(options); return { response: responses.shift() ?? 1 }; },
    fleet, { sleep: async () => {}, pollMs: 0, checkTimeoutMs: 1000, downloadTimeoutMs: 1000 });
  return { manual, calls, dialogs };
}

test('tray check goes through the worker: check every worker, then offer the download it found', async () => {
  // #1623: the tray drove only the desktop's own updater, so Update all workers never reached Unraid.
  const { manual, calls, dialogs } = fleetFixture([
    snap(plan('check')), snap(plan('check', { preparing: true })), snap(plan('download')),
  ], [1]);
  await manual.run();
  assert.deepEqual(calls, [['act', 'check'], ['exchange']]);
  assert.equal(dialogs.length, 1);
  assert.match(dialogs[0].message, /0\.8\.23 is available/);
  assert.match(dialogs[0].detail, /Unraid: Not checked yet; will check and download/);
  assert.deepEqual(dialogs[0].buttons, ['Download update', 'Later']);
  assert.ok(!calls.some(([name]) => name.startsWith('local-')));
});

test('a confirmed download is watched until every worker is staged, then restart is offered', async () => {
  const { manual, calls, dialogs } = fleetFixture([
    snap(plan('download')), snap(plan('download')), snap(plan('check', { preparing: true })),
    snap(plan('install', { staged: 2 })),
  ], [0, 0, 1]);
  await manual.run();
  assert.deepEqual(calls, [['act', 'download'], ['exchange']]);
  assert.equal(dialogs.at(-1).message, 'An update is ready to install');
  assert.match(dialogs.at(-1).detail, /on every worker/);
  assert.deepEqual(dialogs.at(-1).buttons, ['Restart and update', 'Later']);
});

test('restart goes through the worker so every staged worker installs, and a hold is reported', async () => {
  const held = { sent: 'download', held: 'Not every worker is staged yet; nothing was installed',
                 results: [{ device_name: 'Unraid', accepted: true, reason: 'Checking for an update to download' }] };
  const { manual, calls, dialogs } = fleetFixture([snap(plan('install', { staged: 2 }))], [0], { install: held });
  await manual.run();
  assert.deepEqual(calls, [['act', 'install'], ['exchange']]);
  assert.match(dialogs.at(-1).detail, /nothing was installed/);
  assert.ok(!calls.some(([name]) => name === 'local-install'));
});

test('a plan that changed while the dialog was open starts nothing', async () => {
  const { manual, calls, dialogs } = fleetFixture([snap(plan('download')), snap(plan('install'))], [0]);
  await manual.run();
  assert.deepEqual(calls, []);
  assert.equal(dialogs.at(-1).message, 'The update status changed');
});

test('worker refusals and a 409 are shown, not hidden behind a success', async () => {
  const refused = { sent: 'check', results: [{ device_name: 'Unraid', accepted: false, reason: 'Updates disabled' }] };
  let fixture = fleetFixture([snap(plan('check'))], [], { check: refused });
  await fixture.manual.run();
  assert.equal(fixture.dialogs[0].type, 'warning');
  assert.match(fixture.dialogs[0].detail, /Unraid: Updates disabled/);
  fixture = fleetFixture([snap(plan('check'))], [], { check: new Error('Waiting on workers to finish the previous update') });
  await fixture.manual.run();
  assert.match(fixture.dialogs[0].detail, /Waiting on workers/);
});

test('nothing found anywhere reads as up to date, listing each worker', async () => {
  const workers = [{ device_id: 'desk', device_name: 'Desktop', role: 'excluded', label: 'No update offered' }];
  const { manual, dialogs } = fleetFixture([snap(plan('check'), workers)]);
  await manual.run();
  assert.equal(dialogs[0].message, 'AI Shuffle is up to date');
  assert.match(dialogs[0].detail, /Desktop: No update offered/);
});

test('the local updater is used only when the worker plan cannot be read', async () => {
  for (const unreadable of [null, new Error('connection refused')]) {
    const { manual, calls } = fleetFixture([unreadable]);
    await manual.run();
    assert.deepEqual(calls[0], ['local-check']);
    assert.ok(!calls.some(([name]) => name === 'act'));
  }
});

test('turned-off updates and a running install are reported without a request', async () => {
  let fixture = fleetFixture([{ ...snap(plan('download')), allow_updates: false }]);
  await fixture.manual.run();
  assert.deepEqual(fixture.calls, []);
  assert.match(fixture.dialogs[0].message, /turned off/);
  fixture = fleetFixture([snap(plan('check', { waiting: true }))]);
  await fixture.manual.run();
  assert.deepEqual(fixture.calls, []);
  assert.match(fixture.dialogs[0].message, /Waiting on workers/);
});
