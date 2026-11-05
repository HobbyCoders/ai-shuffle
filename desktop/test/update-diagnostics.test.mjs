import { assert, fs, path, createRequire, vm, test, fixture, A, B } from './helpers/source-updates-fixture.mjs';
const require = createRequire(import.meta.url);

test('real signed source stage produces bounded diagnostics, never command or environment text', async t => {
  const f = await fixture(t), updates = f.create(); await updates.load();
  await f.release(B); await updates.download();
  assert.equal(updates.diagnostics.revision, B);
  assert.ok(updates.diagnostics.stages.some(item => item.stage === 'source_download' && item.outcome === 'completed'));
  assert.ok(updates.diagnostics.stages.some(item => item.stage === 'assembly' && item.outcome === 'completed'));
  const recovered = f.create(); await recovered.load();
  assert.deepEqual(recovered.diagnostics, updates.diagnostics);
  const copy = updates.diagnostics; copy.stages.length = 0;
  assert.ok(updates.diagnostics.stages.length);
  assert.ok(updates.diagnostics.total_elapsed_ms >= 0);
  for (const item of updates.diagnostics.stages) assert.deepEqual(Object.keys(item).sort(), ['elapsed_ms', 'outcome', 'stage']);
});

test('resident desktop SHA comes from loaded source path, never app state or environment', async () => {
  const filename = new URL('../dist/main/updater.js', import.meta.url);
  const text = await fs.readFile(filename, 'utf8');
  const timed = require('../dist/main/timed-phase.js');
  const module = { exports: {} };
  vm.runInNewContext(text, { module, exports: module.exports, __dirname: `/updates/versions/${A}/source/desktop/dist/main`,
    process: { env: { GIT_SHA: B }, versions: {}, platform: 'linux' }, console,
    require: id => id === 'electron' ? { app: {} } : id === './timed-phase' ? timed : path });
  const updater = new module.exports.Updater();
  assert.equal(updater.current.updater.revision, A);
  assert.equal(updater.current.running_version, null);
  assert.equal(module.exports.residentUpdaterRevision(`C:\\updates\\versions\\${B}\\source\\desktop\\dist\\main`), B);
  assert.equal(module.exports.residentUpdaterRevision('/packaged/app.asar/dist/main'), null);
  assert.ok(!updater.current.updater.capabilities.includes('refresh-updater-v1'));
});
