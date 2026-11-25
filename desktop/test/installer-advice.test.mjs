import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const { launcherBehind, readContract, runningLauncher, installerRecommended, launcherFileUrl } = require('../dist/main/installer-advice.js');

const HUB = 'b'.repeat(40);
// The hub's installer list, and launcher.json at each revision (`packed`: revision -> launcher version).
function hub(platforms, packed = { [HUB]: 12 }, status = 200) {
  const calls = [];
  const request = async (url, init) => {
    calls.push({ url, init });
    const revision = Object.keys(packed).find(r => url === launcherFileUrl(r));
    if (revision) return { ok: true, text: async () => JSON.stringify({ version: packed[revision], minimum: 4, start: 4 }) };
    if (url.startsWith('https://raw.githubusercontent.com/')) return { ok: false, text: async () => '404: Not Found' };
    return { ok: status === 200, text: async () => JSON.stringify({ format: 1, platforms }) };
  };
  return { request, calls };
}
const options = (overrides = {}) => ({ running: 11, contract: { version: 12, minimum: 4 }, platform: 'linux', ...overrides });

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

test('the installer is recommended when the hub\'s installer packs a newer launcher than the running one', async () => {
  const { request, calls } = hub({ linux: { version: '1.3.10', revision: HUB } });
  assert.equal(await installerRecommended(options({ request })), true);
  assert.match(calls[0].url, /\/v1\/desktop\/installers$/);
  assert.equal(calls[1].url, `https://raw.githubusercontent.com/HobbyCoders/ai-shuffle/${HUB}/desktop/launcher.json`);
  for (const call of calls) { assert.equal(call.init.credentials, 'omit'); assert.equal(call.init.redirect, 'error'); }
  // A partial step up still helps: the installer packs 11, this source 12, the install runs 10.
  assert.equal(await installerRecommended(options({ request: hub({ win: { revision: HUB } }, { [HUB]: 11 }).request, platform: 'win32', running: 10 })), true);
});

test('a nightly ahead of the stable installer is told nothing when that installer packs the same launcher', async () => {
  // v1.3.8 install (launcher 10) updated in-app to a nightly packing 12; the hub's 1.3.9 installer still packs 10.
  const { request } = hub({ linux: { version: '1.3.9', revision: HUB } }, { [HUB]: 10 });
  assert.equal(await installerRecommended(options({ request, running: 10 })), false);
  assert.equal(await installerRecommended(options({ request, running: 11 })), false, 'an older launcher than the running one');
});

test('no recommendation for a current launcher or an unknown contract; the hub is not read then', async () => {
  const unread = hub({ linux: { version: '1.3.10', revision: HUB } });
  assert.equal(await installerRecommended(options({ request: unread.request, running: 12 })), false);
  assert.equal(await installerRecommended(options({ request: unread.request, contract: null })), false);
  assert.equal(await installerRecommended(options({ request: unread.request, platform: 'darwin' })), false);
  assert.equal(unread.calls.length, 0);
});

test('an unreadable or malformed answer is unknown (null), not a recommendation', async () => {
  assert.equal(await installerRecommended(options({ request: hub({}, {}, 503).request })), null);
  assert.equal(await installerRecommended(options({ request: hub({ linux: { revision: 'main' } }).request })), null);
  assert.equal(await installerRecommended(options({ request: hub({ win: { revision: HUB } }).request })), null, 'no installer for this platform');
  assert.equal(await installerRecommended(options({ request: hub({ linux: { revision: HUB } }, {}).request })), null, 'launcher.json not found at that revision');
  assert.equal(await installerRecommended(options({ request: hub({ linux: { revision: HUB } }, { [HUB]: 'twelve' }).request })), null);
  assert.equal(await installerRecommended(options({ request: async () => { throw new Error('offline'); } })), null);
});

test('reads the launcher contract and the running launcher defensively', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'installer-advice-'));
  try {
    const contract = path.join(dir, 'launcher.json');
    await fs.writeFile(contract, JSON.stringify({ version: 12, minimum: 4, start: 4 }));
    assert.deepEqual(await readContract(contract), { version: 12, minimum: 4 });
    await fs.writeFile(contract, JSON.stringify({ version: 3, minimum: 4 }));
    assert.equal(await readContract(contract), null);
    assert.equal(await readContract(path.join(dir, 'missing.json')), null);

    const build = path.join(dir, 'desktop-build.json');
    await fs.writeFile(build, JSON.stringify({ revision: 'a'.repeat(40), sequence: 1, launcher: 9 }));

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
