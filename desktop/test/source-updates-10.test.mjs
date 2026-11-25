import { assert, test, fs, path, A, B, fixture, installedLauncher } from './helpers/source-updates-fixture.mjs';

// Launcher 12: an update that declares the same database level (app/db/compatibility.json)
// as the build it replaces takes no database copy (owner policy, docker/application_updates.py).
// Raw text, so a test can write a level token the way a file might (1.0, 1e0).
const level = value => `{"format": 1, "level": ${value}, "destructive": "${'0'.repeat(64)}"}`;
const contract = version => JSON.stringify({ version, minimum: 4, start: 4 });
async function levelPair(t, { before = 1, after = 1, launcher = 12 } = {}) {
 const f = await fixture(t); const u = f.create(); await u.load();
 await f.release(A, undefined, {}, { 'app/db/compatibility.json': level(before), 'desktop/launcher.json': contract(12) });
 await u.download(); await u.select(); await u.databaseStarting(); await u.healthy();
 const files = { 'desktop/launcher.json': contract(launcher) };
 if (after !== null) files['app/db/compatibility.json'] = level(after);
 await f.release(B, undefined, {}, files); await u.download();
 const calls = [];
 const db = { validate: async () => calls.push('validate'), snapshot: async () => calls.push('snapshot'), restore: async () => calls.push('restore'), discard: async () => calls.push('discard') };
 const journalFile = path.join(f.root, 'restore-point.json');
 const journal = async () => JSON.parse(await fs.readFile(journalFile, 'utf8'));
 return { f, u, db, calls, journal, journalFile, factory: async previous => { assert.equal(previous, A); return db; } };
}

test('same database level: no copy, the update starts, and its health removes the journal without a discard', async t => {
 const { u, calls, journal, journalFile, factory, db } = await levelPair(t);
 await u.prepareRollback(factory, true);
 const j = await journal();
 assert.equal(j.phase, 'ready'); assert.equal(j.compatible, true); assert.equal(j.revision, B); assert.equal(j.previous, A);
 // The launcher's second pass over the same journal checks no copy either.
 await u.prepareRollback(factory, true);
 await u.select(); await u.requireRestorePoint(db); await u.databaseStarting(); await u.healthy();
 await u.finishRollback(factory);
 assert.deepEqual(calls, []);
 await assert.rejects(fs.stat(journalFile), { code: 'ENOENT' });
 assert.equal(u.pendingVersion, null);
});

test('same database level: a failed first start goes back to the previous build over the same data', async t => {
 const { f, u, calls, factory, journalFile } = await levelPair(t);
 await u.prepareRollback(factory, true); await u.select(); await u.databaseStarting(); await u.failed();
 const next = f.create(); await next.load();
 assert.equal(await next.recoverRollback(factory), true);
 assert.deepEqual(calls, [], 'no copy to restore or discard');
 assert.match(await next.rollbackNotice(), /failed to start\. Went back to .*same database format, so your data was kept/);
 const floor = JSON.parse(await fs.readFile(path.join(f.root, 'database-floor.json'), 'utf8'));
 assert.equal(floor.revision, A, 'the fence returns to the previous build');
 assert.equal(next.pendingVersion, null);
 await assert.rejects(fs.stat(journalFile), { code: 'ENOENT' });
 assert.equal((await next.select()).revision, A);
});

for (const [name, options, compatible] of [
 ['a raised database level', { after: 2 }, true],
 ['an update that declares no level', { after: null }, true],
 ['a previous build that declares no level', { before: null }, true],
 // Docker reads these as floats and takes a copy; desktop must agree.
 ['an update whose level is written as a decimal', { after: '1.0' }, true],
 ['an update whose level is written with an exponent', { after: '1e0' }, true],
 ['an update whose own code predates launcher 12', { launcher: 11 }, true],
 ['an installed launcher before 12', {}, false],
]) test(`a database copy is still made for ${name}`, async t => {
 const { f, u, calls, journal, factory } = await levelPair(t, options);
 if (options.before === null) await fs.rm(path.join(f.root, 'versions', A, 'source/app/db/compatibility.json'), { force: true });
 await u.prepareRollback(factory, compatible);
 assert.deepEqual(calls, ['snapshot']);
 const j = await journal();
 assert.equal(j.phase, 'ready'); assert.equal(j.compatible, undefined);
});

test('a compatible journal in a phase no compatible update writes is refused', async t => {
 const { f, u, factory } = await levelPair(t);
 await u.prepareRollback(factory, true);
 const file = path.join(f.root, 'restore-point.json');
 const j = JSON.parse(await fs.readFile(file, 'utf8'));
 for (const changed of [{ phase: 'restoring' }, { phase: 'preparing' }, { late: true }, { compatible: false }]) {
  await fs.writeFile(file, JSON.stringify({ ...j, ...changed }));
  await assert.rejects(u.recoverRollback(factory, true), /Invalid database restore journal/);
 }
});

test('the installed launcher 12 entry skips the copy for an update at the same database level', async t => {
 const { f } = await levelPair(t);
 const launcher = await installedLauncher(f);
 await launcher.start();
 assert.equal(launcher.events.includes('snapshot'), false);
 assert.ok(launcher.events.includes(B), 'the update was started');
 const j = JSON.parse(await fs.readFile(path.join(f.root, 'restore-point.json'), 'utf8'));
 assert.equal(j.compatible, true);
});
