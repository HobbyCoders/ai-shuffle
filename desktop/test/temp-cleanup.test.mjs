// Windows refuses to delete a folder while anything holds it: a child process whose
// cwd it is and that is still exiting, or an antivirus scan of a file just written.
// A one-shot recursive removal then fails the test with EBUSY/EPERM in its cleanup hook
// (launcher-swap's self-test verdicts on hosted windows-latest). Every recursive
// removal in these tests retries, as fs.rm/fs.rmSync do with maxRetries.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const self = fileURLToPath(import.meta.url);
// rm(…) / rmSync(…) / fs.rm(…) calls, with up to two levels of nested parentheses in their arguments.
const REMOVAL = /\brm(?:Sync)?\s*\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\)/g;

function unretriedRemovals(source) {
  return [...source.matchAll(REMOVAL)].map(match => match[0])
    .filter(call => /recursive\s*:\s*true/.test(call) && !/maxRetries\s*:\s*[1-9]/.test(call));
}

function testSources(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) return testSources(file);
    return /\.(?:mjs|cjs|js|ts)$/.test(entry.name) && file !== self ? [file] : [];
  });
}

test('the removal scan finds one-shot recursive removals and accepts retried ones', () => {
  assert.deepEqual(unretriedRemovals("t.after(() => fs.rmSync(dir, { recursive: true, force: true }));"), ['rmSync(dir, { recursive: true, force: true })']);
  assert.deepEqual(unretriedRemovals("await fs.rm(path.join(f.root,'versions',B),{recursive:true});"), ["rm(path.join(f.root,'versions',B),{recursive:true})"]);
  assert.deepEqual(unretriedRemovals("await rm(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });"), []);
  assert.deepEqual(unretriedRemovals("await fs.rm(path.join(root, 'data/postmaster.pid'));"), []);
});

test('every recursive removal in the desktop tests retries a folder Windows still holds', () => {
  const offenders = testSources(here).flatMap(file =>
    unretriedRemovals(fs.readFileSync(file, 'utf8')).map(call => `${path.relative(here, file)}: ${call}`));
  assert.deepEqual(offenders, [], 'add maxRetries/retryDelay to these removals');
});
