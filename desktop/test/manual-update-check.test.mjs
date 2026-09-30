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
