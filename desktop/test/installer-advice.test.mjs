import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const { launcherBehind, readContract, installedRevision, runningLauncher, installerRecommended } = require('../dist/main/installer-advice.js');

const INSTALLED = 'a'.repeat(40);
const HUB = 'b'.repeat(40);
function hub(platforms, status = 200) {
  const calls = [];
  const request = async (url, init) => {
    calls.push({ url, init });
    return { ok: status === 200, text: async () => JSON.stringify({ format: 1, platforms }) };
  };
  return { request, calls };
}
const options = (overrides = {}) => ({ running: 11, contract: { version: 12, minimum: 4 }, version: '1.3.10', installed: INSTALLED, platform: 'linux', ...overrides });

test('a launcher is behind only below this source\'s version, on Windows only when no in-app swap replaces it', () => {
  const contract = { version: 12, minimum: 8 };
  assert.equal(launcherBehind(11, contract, 'linux'), true);
  assert.equal(launcherBehind(3, contract, 'linux'), true, 'Linux never swaps its launcher in-app');
  assert.equal(launcherBehind(12, contract, 'linux'), false);
  assert.equal(launcherBehind(13, contract, 'linux'), false);
  assert.equal(launcherBehind(11, contract, 'win32'), true);
  assert.equal(launcherBehind(7, contract, 'win32'), false, 'below the minimum, Windows takes this source\'s launcher itself');
  assert.equal(launcherBehind(undefined, contract, 'linux'), false, 'profile and development builds have no installed launcher');
  assert.equal(launcherBehind(1, contract, 'darwin'), false);
});

test('the installer is recommended when the hub has a different, not older installer for this platform', async () => {
  const { request, calls } = hub({ linux: { version: '1.3.10', revision: HUB } });
  assert.equal(await installerRecommended(options({ request })), true);
  assert.match(calls[0].url, /\/v1\/desktop\/installers$/);
  assert.equal(calls[0].init.credentials, 'omit');
  assert.equal(calls[0].init.redirect, 'error');
});

test('a nightly running ahead of the hub counts by its release number', async () => {
  const { request } = hub({ win: { version: '1.3.10', revision: HUB } });
  assert.equal(await installerRecommended(options({ request, platform: 'win32', version: '1.3.10-dev.5' })), true);
  assert.equal(await installerRecommended(options({ request, platform: 'win32', version: '1.3.11-dev.1' })), false,
    'an older release than the running one may not carry this launcher');
});

test('no recommendation for the installed build, a current launcher, or an unknown contract; the hub is not read then', async () => {
  const same = hub({ linux: { version: '1.3.10', revision: INSTALLED } });
  assert.equal(await installerRecommended(options({ request: same.request })), false, 'running the same installer again changes nothing');
  const unread = hub({ linux: { version: '1.3.10', revision: HUB } });
  assert.equal(await installerRecommended(options({ request: unread.request, running: 12 })), false);
  assert.equal(await installerRecommended(options({ request: unread.request, contract: null })), false);
  assert.equal(await installerRecommended(options({ request: unread.request, platform: 'darwin' })), false);
  assert.equal(unread.calls.length, 0);
});

test('an unreadable or malformed hub answer is unknown (null), not a recommendation', async () => {
  assert.equal(await installerRecommended(options({ request: hub({}, 503).request })), null);
  assert.equal(await installerRecommended(options({ request: hub({ linux: { version: 'latest', revision: HUB } }).request })), null);
  assert.equal(await installerRecommended(options({ request: hub({ win: { version: '1.3.10', revision: HUB } }).request })), null);
  assert.equal(await installerRecommended(options({ request: async () => { throw new Error('offline'); } })), null);
});

test('reads the launcher contract, the installed build and the running launcher defensively', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'installer-advice-'));
  try {
    const contract = path.join(dir, 'launcher.json');
    await fs.writeFile(contract, JSON.stringify({ version: 12, minimum: 4, start: 4 }));
    assert.deepEqual(await readContract(contract), { version: 12, minimum: 4 });
    await fs.writeFile(contract, JSON.stringify({ version: 3, minimum: 4 }));
    assert.equal(await readContract(contract), null);
    assert.equal(await readContract(path.join(dir, 'missing.json')), null);

    const build = path.join(dir, 'desktop-build.json');
    await fs.writeFile(build, JSON.stringify({ revision: INSTALLED, sequence: 1, launcher: 9 }));
    assert.equal(await installedRevision(build), INSTALLED);
    assert.equal(await installedRevision(path.join(dir, 'missing.json')), null);

    assert.equal(await runningLauncher('11', dir, build, 'win32'), 11);
    assert.equal(await runningLauncher(undefined, dir, build, 'win32'), 0, 'Windows launchers before the stamp pass nothing');
    // Linux: the installed app.asar's launcher.json (here the contract file, version 3), then the stamp.
    assert.equal(await runningLauncher(undefined, dir, build, 'linux'), 3);
    await fs.rm(contract);
    assert.equal(await runningLauncher(undefined, dir, build, 'linux'), 9);
    assert.equal(await runningLauncher(undefined, dir, path.join(dir, 'missing.json'), 'linux'), 0);
  } finally {
    await fs.rm(dir, { recursive: true, force: true });
  }
});
