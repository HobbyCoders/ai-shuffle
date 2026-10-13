// electron-builder's afterPack hook: verify the files that enter every real
// installer, after extraResources has copied them out of the runtime staging area.
const { createHash } = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');

module.exports = async function verifyPackagedRuntime(context) {
  const resources = context.packager.getResourcesDir(context.appOutDir);
  const worker = path.join(resources, 'worker');
  const manifest = JSON.parse(await fs.readFile(path.join(worker, 'manifest.json'), 'utf8'));
  // PR shell builds intentionally contain only the explicit stub manifest.
  if (manifest.stub === true) return;
  const client = manifest.cloudflared;
  if (!client?.version || !/^[a-f0-9]{64}$/.test(client.sha256 ?? '')) {
    throw new Error('Packaged worker is missing verified cloudflared metadata. Rebuild the worker runtime.');
  }
  const expected = `server/downloads/cloudflared/${client.version}/cloudflared${context.electronPlatformName === 'win32' ? '.exe' : ''}`;
  if (client.path !== expected || !/^\d+\.\d+\.\d+$/.test(client.version)) {
    throw new Error('Packaged cloudflared path is invalid. Rebuild the worker runtime.');
  }
  const binary = path.join(worker, client.path);
  if (!(await fs.stat(binary)).isFile()) throw new Error('Packaged cloudflared is not a file.');
  const digest = createHash('sha256').update(await fs.readFile(binary)).digest('hex');
  if (digest !== client.sha256) throw new Error('Packaged cloudflared checksum does not match the verified runtime.');
};
