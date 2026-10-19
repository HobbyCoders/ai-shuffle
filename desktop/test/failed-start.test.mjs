import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { test } from 'node:test';
const require = createRequire(import.meta.url);
const { retryableStarts, releaseStarts, isFirstStart, installerNewer, MAX_START_RETRIES, RETRIES_FILE } = require('../dist/main/failed-start.js');

const A = 'a'.repeat(40), B = 'b'.repeat(40), C = 'c'.repeat(40);
const text = value => JSON.stringify(value, null, 2) + '\n';
async function root(t, state, floor = { schemaVersion: 1, revision: A, sequence: 100 }) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-failed-start-'));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  if (state !== undefined) await fs.writeFile(path.join(dir, 'state.json'), typeof state === 'string' ? state : text(state));
  if (floor) await fs.writeFile(path.join(dir, 'database-floor.json'), text(floor));
  return dir;
}
// What a current launcher leaves after it put a failed first start back (recoverRollback).
const rolledBack = { schemaVersion: 1, previous: C, supersededFailed: [B], failed: [B] };

test('the state a launcher leaves after putting back a first start that never reported back is retryable', async t => {
  const dir = await root(t, rolledBack);
  assert.deepEqual(await retryableStarts(dir), [B]);
  assert.deepEqual(await releaseStarts(dir, [B]), [B]);
  const state = JSON.parse(await fs.readFile(path.join(dir, 'state.json'), 'utf8'));
  // Out of failed; supersededFailed and the rest are left as the launcher wrote them.
  assert.deepEqual(state, { schemaVersion: 1, previous: C, supersededFailed: [B] });
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(dir, RETRIES_FILE), 'utf8')), { [B]: 1 });
  // Written in the updater's own serialization, so the launcher still reads it as its own write.
  assert.equal(await fs.readFile(path.join(dir, 'state.json'), 'utf8'), text(state));
  assert.deepEqual(await retryableStarts(dir), []);
});

test('only the requested revisions are released, and other failed builds stay refused', async t => {
  const dir = await root(t, { ...rolledBack, supersededFailed: [B, C], failed: [C, B] });
  assert.deepEqual(await releaseStarts(dir, [B]), [B]);
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(dir, 'state.json'), 'utf8')).failed, [C]);
  assert.deepEqual(await releaseStarts(dir, ['f'.repeat(40)]), []);
});

test(`a revision is offered again at most ${MAX_START_RETRIES} times`, async t => {
  const dir = await root(t, rolledBack);
  for (let attempt = 1; attempt <= MAX_START_RETRIES; attempt++) {
    assert.deepEqual(await releaseStarts(dir, [B]), [B], `retry ${attempt}`);
    // It fails again and the launcher puts it back again.
    await fs.writeFile(path.join(dir, 'state.json'), text(rolledBack));
  }
  assert.deepEqual(await retryableStarts(dir), []);
  assert.deepEqual(await releaseStarts(dir, [B]), []);
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(dir, 'state.json'), 'utf8')).failed, [B]);
});

test('anything the launcher has not settled, fenced or cannot prove is never released', async t => {
  const cases = {
    'booting': { ...rolledBack, booting: B },
    'pending': { ...rolledBack, pending: B },
    'waiting': { ...rolledBack, waiting: B },
    'blocked': { ...rolledBack, blocked: true },
    'fenced (may have migrated)': { ...rolledBack, mayHaveMigrated: { [B]: 101 } },
    'recovery required': { ...rolledBack, recoveryRequired: true },
    'recovered from a damaged state file': { ...rolledBack, recoveredState: true },
    // Failed, but never put back by a restore point (no supersededFailed entry).
    'not put back': { schemaVersion: 1, failed: [B] },
  };
  for (const [name, state] of Object.entries(cases)) {
    const dir = await root(t, state);
    assert.deepEqual(await retryableStarts(dir), [], name);
    assert.deepEqual(await releaseStarts(dir, [B]), [], name);
    assert.equal(await fs.readFile(path.join(dir, 'state.json'), 'utf8'), text(state), name);
  }
  // A state file another writer produced (not this updater's serialization).
  assert.deepEqual(await retryableStarts(await root(t, JSON.stringify(rolledBack))), [], 'legacy write');
  assert.deepEqual(await retryableStarts(await root(t, { ...rolledBack, schemaVersion: undefined })), [], 'no schema');
  assert.deepEqual(await retryableStarts(await root(t, '{')), [], 'unreadable');
  assert.deepEqual(await retryableStarts(await root(t, undefined)), [], 'no state');
  // An unfinished restore point: the database may not be back yet.
  const journal = await root(t, rolledBack);
  await fs.writeFile(path.join(journal, 'restore-point.json'), '{}');
  assert.deepEqual(await retryableStarts(journal), [], 'restore point');
  // The database floor names the failed build (it opened the database and was not put back), or cannot be read.
  assert.deepEqual(await retryableStarts(await root(t, rolledBack, { schemaVersion: 1, revision: B, sequence: 101 })), [], 'floor is the failed build');
  assert.deepEqual(await retryableStarts(await root(t, rolledBack, { schemaVersion: 1, revision: A, sequence: 100, uncertain: true })), [], 'uncertain floor');
  assert.deepEqual(await retryableStarts(await root(t, rolledBack, { revision: A })), [], 'invalid floor');
  // A bundled previous version that never wrote a floor is fine.
  assert.deepEqual(await retryableStarts(await root(t, rolledBack, null)), [B], 'no floor');
});

test('an update is on its first start only while the launcher has it booting', async t => {
  assert.equal(await isFirstStart(await root(t, { schemaVersion: 1, active: A, pending: B, booting: B, waiting: B }), B), true);
  assert.equal(await isFirstStart(await root(t, { schemaVersion: 1, active: B }), B), false);
  assert.equal(await isFirstStart(await root(t, undefined), B), false);
});

test('the installer is pointed to only when the hub has a newer release for this platform', async () => {
  const requests = [];
  const hub = body => async (url, init) => { requests.push([url, init.redirect]); return new Response(typeof body === 'string' ? body : JSON.stringify(body)); };
  // The hub's list today: the Windows entry carries no signed payload, only its version.
  const list = { format: 1, platforms: { win: { platform: 'win', version: '1.0.1', revision: 'f'.repeat(40) }, linux: { version: '1.0.3' } } };
  assert.equal(await installerNewer('1.0.1', 'win32', hub(list)), false);
  assert.equal(await installerNewer('1.0.2', 'win32', hub(list)), false);
  assert.equal(await installerNewer('1.0.0', 'win32', hub(list)), true);
  assert.equal(await installerNewer('1.0.2', 'linux', hub(list)), true);
  assert.deepEqual(requests[0], ['https://hobbycoders.com/v1/desktop/installers', 'error']);
  // Unknown: no entry, a malformed version, an oversized or broken reply, a failed request, an unknown platform.
  assert.equal(await installerNewer('1.0.1', 'darwin', hub(list)), null);
  assert.equal(await installerNewer('1.0.1', 'win32', hub({ platforms: { win: { version: 'latest' } } })), null);
  assert.equal(await installerNewer('1.0.1', 'win32', hub('x'.repeat(70 * 1024))), null);
  assert.equal(await installerNewer('1.0.1', 'win32', hub('{')), null);
  assert.equal(await installerNewer('1.0.1', 'win32', async () => new Response('', { status: 503 })), null);
  assert.equal(await installerNewer('1.0.1', 'win32', async () => { throw Error('offline'); }), null);
  assert.equal(await installerNewer('1.0.1', 'aix', hub(list)), null);
});
