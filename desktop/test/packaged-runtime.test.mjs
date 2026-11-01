import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { getConfig, validateConfiguration } from './helpers/builder-config.mjs';

const require = createRequire(import.meta.url);
const verify = require('../scripts/verify-packaged-runtime.cjs');

test('standard and preview installers verify cloudflared after copying extraResources', async () => {
  const project = fileURLToPath(new URL('..', import.meta.url));
  for (const filename of ['electron-builder.yml', 'electron-builder.preview.yml']) {
    const config = await getConfig(project, filename, null);
    await validateConfiguration(config, { isEnabled: false });
    assert.equal(config.afterPack, 'scripts/verify-packaged-runtime.cjs');
    assert(config.extraResources.some(entry => entry.from === 'worker' && entry.to === 'worker'));
    assert(config.win.target.some(target => target.target === 'nsis'));
  }
});

test('Windows signing skips cloudflared so the byte-exact check survives signed builds (#976)', async () => {
  // electron-builder's own filter, applied to resources while extraResources
  // are copied (before afterPack) and to the app directory afterwards.
  const { WinPackager } = require('app-builder-lib');
  const project = fileURLToPath(new URL('..', import.meta.url));
  const resources = path.join('release', 'win-unpacked', 'resources');
  for (const filename of ['electron-builder.yml', 'electron-builder.preview.yml']) {
    const config = await getConfig(project, filename, null);
    const signs = file => WinPackager.prototype.shouldSignFile.call({ platformSpecificBuildOptions: config.win }, file);
    assert.equal(signs(path.join(resources, 'worker', 'server', 'downloads', 'cloudflared', '2026.8.3', 'cloudflared.exe')), false, filename);
    for (const file of [path.join('release', 'win-unpacked', 'AI Shuffle.exe'), path.join(resources, 'worker', 'node', 'node.exe'),
      path.join(resources, 'worker', 'bin', 'gh.exe'), path.join(resources, 'worker', 'python', 'python.exe'),
      path.join(resources, 'postgres', 'bin', 'postgres.exe')]) {
      assert.equal(signs(file), true, `${filename}: ${file}`);
    }
    assert.equal(signs(path.join(resources, 'worker', 'python', 'python313.dll')), false, `${filename}: DLLs stay unsigned`);
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
    await rm(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  }
});
