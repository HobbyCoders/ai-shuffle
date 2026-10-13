/** Explicit local test profiles. Stable installs can never silently opt into a test feed. */
const fs = require('node:fs');
const path = require('node:path');
const { createPublicKey } = require('node:crypto');
function within(a, b) { const relative = path.relative(a, b); return relative === '' || (!relative.startsWith('..') && !path.isAbsolute(relative)); }
function canonical(file) {
  const suffix = []; let ancestor = path.resolve(file);
  while (!fs.existsSync(ancestor)) { suffix.unshift(path.basename(ancestor)); const parent = path.dirname(ancestor); if (parent === ancestor) throw Error('Cannot resolve isolated root'); ancestor = parent; }
  return path.join(fs.realpathSync(ancestor), ...suffix);
}
function isolatedRoot(root, env = process.env) {
  root = canonical(root);
  const protectedRoots = [
    env.LOCALAPPDATA && path.join(env.LOCALAPPDATA, 'AI Shuffle Source'),
    env.APPDATA && path.join(env.APPDATA, 'ai-shuffle-desktop'),
    env.USERPROFILE && path.join(env.USERPROFILE, '.ai-shuffle')
  ].filter(Boolean).map(canonical);
  if (protectedRoots.some(stable => within(stable, root) || within(root, stable))) throw Error('An isolated profile cannot overlap the normal application or its data');
  return root;
}
function validateProfile(root, value) {
  isolatedRoot(root);
  if (value?.format !== 1 || value.isolated !== true) throw Error('Invalid isolated profile');
  const ports = [value.port, value.protectedPort, ...(value.debugPort === undefined ? [] : [value.debugPort])];
  if (ports.some(port => !Number.isSafeInteger(port) || port < 1024 || port > 65535 || [8000, 8001].includes(port)) || new Set(ports).size !== ports.length) throw Error('Isolated ports must be distinct, unprivileged, and different from 8000/8001');
  if (Boolean(value.feedDirectory) !== Boolean(value.publicKey)) throw Error('A local test feed requires its own public key');
  if (value.feedDirectory) {
    if (typeof value.feedDirectory !== 'string' || !path.isAbsolute(value.feedDirectory) || !within(root, value.feedDirectory)) throw Error('Test feeds must be inside their isolated root');
    if (typeof value.publicKey !== 'string' || createPublicKey(value.publicKey).asymmetricKeyType !== 'ed25519') throw Error('Test feeds require an Ed25519 public key');
  }
  return value;
}
function readProfile(root) {
  const file = path.join(root, 'launch-profile.json');
  if (!fs.existsSync(file)) return null;
  return validateProfile(root, JSON.parse(fs.readFileSync(file, 'utf8')));
}
function createProfile(root, options) {
  isolatedRoot(root);
  if (Boolean(options.feedDirectory) !== Boolean(options.publicKeyFile)) throw Error('A local test feed requires both --feed-directory and --public-key');
  if (fs.existsSync(path.join(root, 'state.json')) || fs.existsSync(path.join(root, 'launch-profile.json'))) throw Error('The launch profile must be configured before the first installation');
  const profile = validateProfile(root, { format: 1, isolated: true, port: options.port, protectedPort: options.protectedPort, ...(options.debugPort !== undefined ? { debugPort: options.debugPort } : {}), ...(options.feedDirectory ? { feedDirectory: path.resolve(options.feedDirectory), publicKey: fs.readFileSync(options.publicKeyFile, 'utf8') } : {}) });
  fs.mkdirSync(root, { recursive: true });
  fs.writeFileSync(path.join(root, 'launch-profile.json'), JSON.stringify(profile), { flag: 'wx', mode: 0o600 });
  return profile;
}
function localFeed(profile) {
  if (!profile?.isolated || !profile.feedDirectory) return undefined;
  return async url => {
    const target = new URL(String(url));
    if (target.origin !== 'https://hobbycoders.com' || target.search || target.hash) throw Error('Unexpected test feed request');
    const revision = target.pathname.match(/^\/v1\/desktop\/sources\/([a-f0-9]{40})$/)?.[1];
    const name = ['/v1/desktop/manifest', '/v1/desktop/manifest/edge'].includes(target.pathname) ? 'latest.json' : revision ? `${revision}.zip` : null;
    if (!name) throw Error('Unexpected test feed request');
    try { return new Response(await fs.promises.readFile(path.join(profile.feedDirectory, name))); }
    catch (error) { if (error.code === 'ENOENT') return new Response('Unpublished test revision', { status: 404 }); throw error; }
  };
}
module.exports = { readProfile, createProfile, validateProfile, isolatedRoot, localFeed };
