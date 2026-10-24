import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { GH_VERSION, GH_ASSETS } from '../scripts/gh-binaries.mjs';
import { POSTGRES_VERSION } from '../scripts/postgres-binaries.mjs';
const { nativeRuntimeKey, prepareNativeRuntime } = createRequire(import.meta.url)('../dist/main/source-runtime.js');

test('native runtime cache depends on pinned inputs, not build-script text or JSON formatting', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-native-key-'));
  t.after(() => fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }));
  const source = path.join(root, 'source'), inputs = new URL('../runtime-contract.json', import.meta.url);
  const contract = JSON.parse(await fs.readFile(inputs, 'utf8'));
  await fs.mkdir(path.join(source, 'desktop/scripts'), { recursive: true });
  const file = path.join(source, 'desktop/runtime-contract.json');
  await fs.writeFile(file, JSON.stringify(contract));
  const key = await nativeRuntimeKey(source);
  for (const script of ['build-worker.mjs', 'postgres-binaries.mjs', 'gh-binaries.mjs']) await fs.writeFile(path.join(source, 'desktop/scripts', script), '// a runtime-script edit');
  assert.equal(await nativeRuntimeKey(source), key);
  await fs.writeFile(file, JSON.stringify(contract, null, 4) + '\r\n');
  assert.equal(await nativeRuntimeKey(source), key);
  // A cached runtime must be used without running any build script.
  const worker = path.join(root, 'updates/runtimes', key.slice(0, 24), 'worker');
  await fs.mkdir(worker, { recursive: true });
  await fs.writeFile(path.join(worker, 'manifest.json'), JSON.stringify({ pythonDependencies: 'locks' }));
  const prepared = await prepareNativeRuntime(source, path.join(root, 'updates'), 'must-not-execute', () => assert.fail('runtime rebuilt'));
  assert.equal(prepared.key, key);
  for (const [field, value] of [['node', '22.99.0'], ['postgres', '16.99.0'], ['pip', '99.0']]) {
    await fs.writeFile(file, JSON.stringify({ ...contract, native: { ...contract.native, [field]: value } }));
    assert.notEqual(await nativeRuntimeKey(source), key, field);
  }
  const python = structuredClone(contract); python.native.python.build = 'next-build';
  await fs.writeFile(file, JSON.stringify(python)); assert.notEqual(await nativeRuntimeKey(source), key);
  const gh = structuredClone(contract); gh.native.gh.assets['win32-x64'][1] = 'f'.repeat(64);
  await fs.writeFile(file, JSON.stringify(gh)); assert.notEqual(await nativeRuntimeKey(source), key);
  assert.equal(GH_VERSION, contract.native.gh.version);
  assert.deepEqual(GH_ASSETS, contract.native.gh.assets);
  assert.equal(POSTGRES_VERSION, contract.native.postgres);
});
