import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { createRequire } from 'node:module';
import { afterRename } from './helpers/crash-rename.mjs';
import { spawn } from 'node:child_process';
const { ColdClusterCopy, durableJson, durableRename, WINDOWS_MOVE_SCRIPT } = createRequire(import.meta.url)('../dist/main/database-restore.js');
async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-restore-'));
  t.after(() => fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
  await fs.mkdir(path.join(root, 'data/global'), { recursive: true });
  await fs.mkdir(path.join(root, 'data/empty'));
  await fs.writeFile(path.join(root, 'data/PG_VERSION'), '16');
  await fs.writeFile(path.join(root, 'data/global/pg_control'), 'clean control');
  await fs.writeFile(path.join(root, 'data/rows'), 'previous schema and data');
  const calls = [];
  return { root, calls, copy: new ColdClusterCopy(root, async snapshot => { calls.push(snapshot); }) };
}
test('cold copy is validated, durable, private and restores files and empty directories', async t => {
  const { root, copy, calls } = await fixture(t);
  await copy.snapshot();
  await fs.writeFile(path.join(root, 'data/rows'), 'migrated data');
  await copy.restore();
  assert.equal(await fs.readFile(path.join(root, 'data/rows'), 'utf8'), 'previous schema and data');
  assert.ok((await fs.stat(path.join(root, 'data/empty'))).isDirectory());
  assert.deepEqual(calls, [true, false]);
  assert.ok(await fs.stat(path.join(root, 'update-snapshot/manifest.json')));
  await copy.discard();
  assert.deepEqual(await fs.readdir(root), ['data']);
});
test('missing/corrupt copy and a live pid never overwrite the database', async t => {
  const { root, copy } = await fixture(t);
  await fs.writeFile(path.join(root, 'data/postmaster.pid'), '123');
  await assert.rejects(copy.snapshot(), /stopped/);
  await copy.discard();
  await fs.rm(path.join(root, 'data/postmaster.pid'));
  await copy.snapshot();
  await fs.writeFile(path.join(root, 'update-snapshot/data/rows'), 'corrupt');
  await assert.rejects(copy.restore(), /validation/);
  assert.equal(await fs.readFile(path.join(root, 'data/rows'), 'utf8'), 'previous schema and data');
});
test('snapshot failure is closed, and only one snapshot can be retained', async t => {
  const { root, copy } = await fixture(t);
  const denied = new ColdClusterCopy(root, async () => { throw Error('worker alive'); });
  await assert.rejects(denied.snapshot(), /worker alive/);
  await assert.rejects(fs.stat(path.join(root, 'update-snapshot')), { code: 'ENOENT' });
  await copy.snapshot();
  await assert.rejects(copy.snapshot(), { code: 'EEXIST' });
});
for (const boundary of ['data', 'update-restore']) test(`restore resumes after power loss just after renaming ${boundary}`, async t => {
  const { root, copy } = await fixture(t);
  await copy.snapshot();
  await fs.writeFile(path.join(root, 'data/rows'), 'migrated');
  const undo = afterRename(async from => {
    if (from === path.join(root, boundary)) throw Error('power loss');
  });
  try { await assert.rejects(copy.restore(), /power loss/); } finally { undo(); }
  const restarted = new ColdClusterCopy(root, async () => {});
  await restarted.restore();
  assert.equal(await fs.readFile(path.join(root, 'data/rows'), 'utf8'), 'previous schema and data');
  await restarted.discard();
});
test('an interrupted restore copy keeps the immutable snapshot and retries', async t => {
  const { root, copy } = await fixture(t);
  await copy.snapshot();
  const copyFile = fs.copyFile;
  fs.copyFile = async (from, to) => { if (to.startsWith(path.join(root, 'update-restore'))) throw Error('disk full'); return copyFile(from, to); };
  try { await assert.rejects(copy.restore(), /disk full/); } finally { fs.copyFile = copyFile; }
  assert.ok(await fs.stat(path.join(root, 'update-snapshot/manifest.json')));
  await copy.restore();
});
test('journal replacement flushes its temporary file before publishing it', async t => {
  const { root } = await fixture(t);
  const file = path.join(root, 'journal.json');
  await durableJson(file, { phase: 'preparing' });
  const open = fs.open;
  fs.open = async (...args) => {
    const handle = await open(...args);
    if (args[0] === file + '.tmp') handle.sync = async () => { throw Error('power loss'); };
    return handle;
  };
  try { await assert.rejects(durableJson(file, { phase: 'ready' }), /power loss/); } finally { fs.open = open; }
  assert.deepEqual(JSON.parse(await fs.readFile(file)), { phase: 'preparing' });
  await durableJson(file, { phase: 'ready' });
  assert.deepEqual(JSON.parse(await fs.readFile(file)), { phase: 'ready' });
});

test('the Windows move retries only transient sharing errors, for a bounded time', () => {
  assert.match(WINDOWS_MOVE_SCRIPT, /MoveFileEx\(from, to, 9\)/);
  assert.match(WINDOWS_MOVE_SCRIPT, /error != 5 && error != 32 && error != 33\) return error/);
  assert.match(WINDOWS_MOVE_SCRIPT, /attempt <= 40/);
  // Read in C#, right after the call: PowerShell overwrites the last error.
  assert.match(WINDOWS_MOVE_SCRIPT, /Marshal\.GetLastWin32Error\(\)/);
});
const windowsOnly = { skip: process.platform !== 'win32' && 'MoveFileEx is Windows only' };
test('a Windows move waits for a file another process holds open', windowsOnly, async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-move-'));
  t.after(() => fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
  const file = path.join(root, 'state.json.tmp'), target = path.join(root, 'state.json');
  await fs.writeFile(file, '{}');
  // No FILE_SHARE_DELETE, like a scanner's handle: the move fails until it closes.
  const holder = spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
    '$h = [IO.File]::Open($env:HELD, "Open", "Read", "Read"); [Console]::Out.WriteLine("held"); Start-Sleep -Seconds 3; $h.Close()'],
  { env: { ...process.env, HELD: file }, stdio: ['ignore', 'pipe', 'inherit'] });
  t.after(() => holder.kill());
  await new Promise((resolve, reject) => { holder.stdout.on('data', d => { if (String(d).includes('held')) resolve(); }); holder.on('exit', reject); });
  const started = Date.now();
  await durableRename(file, target);
  assert.ok(Date.now() - started >= 1000, 'the move waited for the handle');
  assert.equal(await fs.readFile(target, 'utf8'), '{}');
});
test('a Windows move that cannot succeed reports the real Windows error at once', windowsOnly, async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-move-'));
  t.after(() => fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
  const started = Date.now();
  await assert.rejects(durableRename(path.join(root, 'missing'), path.join(root, 'moved')), /Windows error 2: /);
  assert.ok(Date.now() - started < 30_000);
});
