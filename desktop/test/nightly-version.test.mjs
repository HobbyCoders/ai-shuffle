import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { nightlyVersion } from '../scripts/nightly-version.mjs';

const resolver = fileURLToPath(new URL('../../android/ci/resolve-version-name.py', import.meta.url));

// main: v1.1.0, a merged side branch, then the version bump that is tagged v1.2.0
// while its nightly is in flight (the 1.2.0 release, 2026-11-01).
async function history(t) {
 const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-nightly-version-'));
 t.after(() => fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
 const env = { ...process.env, GIT_AUTHOR_NAME: 'fixture', GIT_AUTHOR_EMAIL: 'fixture@example.invalid',
  GIT_COMMITTER_NAME: 'fixture', GIT_COMMITTER_EMAIL: 'fixture@example.invalid', GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: os.devnull };
 const git = (...args) => execFileSync('git', ['-C', root, ...args], { encoding: 'utf8', env }).trim();
 const commit = message => git('commit', '--allow-empty', '-q', '-m', message);
 git('init', '-q', '-b', 'main');
 commit('base');
 git('tag', 'v1.1.0');
 commit('feature');
 git('checkout', '-q', '-b', 'side');
 commit('side 1'); commit('side 2');
 git('checkout', '-q', 'main');
 git('merge', '-q', '--no-ff', '-m', 'merge side', 'side');
 commit('Release 1.2.0');
 const describe = () => execFileSync('python', ['-I', '-c',
  'import importlib.util,sys; s=importlib.util.spec_from_file_location("r",sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.version_name())', resolver],
  { cwd: root, encoding: 'utf8', env }).trim();
 return { git, describe, revision: git('rev-parse', 'HEAD') };
}

test('a release tag pushed mid-run keeps the version the gate pinned for the UI bundle', async t => {
 const { git, describe, revision } = await history(t);
 const pinned = describe();
 assert.match(pinned, /^1\.1\.0-dev\.3\+g[0-9a-f]{7,}$/);
 git('tag', 'v1.2.0');
 const resolved = describe();
 assert.equal(resolved, '1.2.0');
 assert.equal(nightlyVersion({ resolved, pinned, revision, git }), pinned);
});

test('without a pin, or when nothing moved, the signer keeps its own resolution', async t => {
 const { git, describe, revision } = await history(t);
 const resolved = describe();
 assert.equal(nightlyVersion({ resolved, pinned: '', revision, git }), resolved);
 assert.equal(nightlyVersion({ resolved, pinned: resolved, revision, git }), resolved);
});

test('a pinned version that does not describe this exact revision is refused', async t => {
 const { git, describe, revision } = await history(t);
 const pinned = describe();
 git('tag', 'v1.2.0');
 const resolved = describe();
 const abbrev = revision.slice(0, 9);
 for (const forged of ['9.9.9', '1.1.0', `1.1.0-dev.4+g${abbrev}`, `1.1.0-dev.2+g${abbrev}`, `1.0.0-dev.3+g${abbrev}`,
  pinned.replace(/\+g.*/, '+gdeadbee'), `1.2.0-dev.1+g${abbrev}`]) {
  assert.throws(() => nightlyVersion({ resolved, pinned: forged, revision, git }), /Pinned version/, forged);
 }
});
