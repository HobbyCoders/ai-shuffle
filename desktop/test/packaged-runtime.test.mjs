import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const verify = require('../scripts/verify-packaged-runtime.cjs');

test('standard and preview installers verify cloudflared after copying extraResources', async () => {
  const { getConfig, validateConfiguration } = require('app-builder-lib/out/util/config/config');
  const project = fileURLToPath(new URL('..', import.meta.url));
  for (const filename of ['electron-builder.yml', 'electron-builder.preview.yml']) {
    const config = await getConfig(project, filename, null);
    await validateConfiguration(config, { isEnabled: false });
    assert.equal(config.afterPack, 'scripts/verify-packaged-runtime.cjs');
    assert(config.extraResources.some(entry => entry.from === 'worker' && entry.to === 'worker'));
    assert(config.win.target.some(target => target.target === 'nsis'));
  }
});

test('real installer resources require the verified client; explicit shell stubs may omit it', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-package-'));
  const worker = path.join(dir, 'resources', 'worker');
  const manifest = path.join(worker, 'manifest.json');
  const context = { appOutDir: dir, electronPlatformName: 'win32', packager: { getResourcesDir: out => path.join(out, 'resources') } };
  const bytes = Buffer.from('verified cloudflared fixture');
  const client = {
    version: '2026.8.3', path: 'server/downloads/cloudflared/2026.8.3/cloudflared.exe',
    sha256: createHash('sha256').update(bytes).digest('hex'),
  };
  try {
    await mkdir(worker, { recursive: true });
    await writeFile(manifest, JSON.stringify({ stub: true }));
    await verify(context);
    await writeFile(manifest, JSON.stringify({ stub: false }));
    await assert.rejects(verify(context), /missing verified cloudflared/);
    await writeFile(manifest, JSON.stringify({ stub: false, cloudflared: client }));
    await assert.rejects(verify(context), { code: 'ENOENT' });
    const binary = path.join(worker, client.path);
    await mkdir(path.dirname(binary), { recursive: true });
    await writeFile(binary, bytes);
    await verify(context);
    await writeFile(binary, 'corrupted resource copy');
    await assert.rejects(verify(context), /checksum/);
    await writeFile(manifest, JSON.stringify({ cloudflared: { ...client, path: '../cloudflared.exe' } }));
    await assert.rejects(verify(context), /path is invalid/);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
