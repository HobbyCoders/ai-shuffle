// Capture this journey's real spec/helpers without launching the shared runner.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const journeyUrl = new URL('../update-runtime.journey.mjs', import.meta.url);
const source = readFileSync(journeyUrl, 'utf8');
const runnerImport = "import { runJourney } from './runner.mjs';";
assert.ok(source.includes(runnerImport));
const key = '__updateRuntimeFixtureRegressionSpec';
const isolated = source.replace(runnerImport, `const runJourney = async (spec) => { globalThis.${key} = spec; };`)
  .replace(/from '(\.\/[^']+)'/g, (_match, path) => `from '${new URL(path, journeyUrl).href}'`);
const journey = await import(`data:text/javascript;base64,${Buffer.from(isolated).toString('base64')}`);
const spec = globalThis[key];
delete globalThis[key];

test('only this HTTP-fixture journey requests a service-worker-blocked context', async () => {
  assert.deepEqual(await spec.browser({}), { context: { serviceWorkers: 'block' } });
  assert.equal(spec.cases.length, 11, 'retain every native/controlled/read-only case');
});

for (const key of ['statusGet', 'workersGet', 'checkPost']) {
  const payload = key === 'statusGet' ? { running_version: 'a'.repeat(40) }
    : key === 'workersGet' ? { workers: [{ device_id: 'peer' }] }
    : { results: [{ device_id: 'peer', accepted: false, reason: 'Offline; not queued' }] };
  const response = (body = payload) => ({ ok: () => true, fromServiceWorker: () => false, json: async () => body });
  test(`${key}: a missing/bypassed route fails even if a response looks correct`, async () => {
    await assert.rejects(async () => journey.assertFixtureResponse({ fixtureHits: { [key]: 0 } }, key, response(), payload), /fixture .* was not intercepted/);
  });
  test(`${key}: a hit with the native/unexpected body fails readback`, async () => {
    await assert.rejects(async () => journey.assertFixtureResponse({ fixtureHits: { [key]: 1 } }, key, response({ updater: { revision: null }, diagnostics: null }), payload), /fixture .* readback did not match/);
  });
  test(`${key}: a real hit and exact readback pass`, async () => {
    const messages = [];
    await journey.assertFixtureResponse({ fixtureHits: { [key]: 1 }, log: (message) => messages.push(message) }, key, response(), payload);
    assert.match(messages.join('\n'), /hits=1 readback=matched/);
  });
}

test('a service-worker response is not accepted even with a prior fixture hit', async () => {
  await assert.rejects(async () => journey.assertFixtureResponse({ fixtureHits: { statusGet: 1 } }, 'statusGet', { ok: () => true, fromServiceWorker: () => true }, {}), /fixture .* was served by a service worker/);
});

test('an intercepted HTTP failure is not accepted as fixture evidence', async () => {
  await assert.rejects(async () => journey.assertFixtureResponse({ fixtureHits: { statusGet: 1 } }, 'statusGet', { ok: () => false }, {}), /fixture .* response failed/);
});
