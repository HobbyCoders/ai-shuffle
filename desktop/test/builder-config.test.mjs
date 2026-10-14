import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const desktop = fileURLToPath(new URL('..', import.meta.url));

test('real builder config loading keeps diagnostics off the runner event pipe', () => {
  // A plain process exposes ONLY the dependency's output, never TAP or test
  // events. This fails without the sink redirect regardless of pipe timing.
  const result = spawnSync(process.execPath, ['--input-type=module', '-e', `
    import assert from 'node:assert/strict';
    import { getConfig, validateConfiguration } from './test/helpers/builder-config.mjs';
    for (const filename of ['electron-builder.yml', 'electron-builder.preview.yml']) {
      const config = await getConfig(process.cwd(), filename, null);
      await validateConfiguration(config, { isEnabled: false });
      assert.equal(config.afterPack, 'scripts/verify-packaged-runtime.cjs');
    }
    await assert.rejects(() => validateConfiguration({ unknownConfigurationKey: true }, { isEnabled: false }));
  `], { cwd: desktop, encoding: 'utf8', timeout: 30000 });
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, '', 'builder logs must not share stdout with serialized test events');
  assert.match(result.stderr, /loaded configuration.*electron-builder\.yml/);
  assert.match(result.stderr, /loaded configuration.*electron-builder\.preview\.yml/);
});
