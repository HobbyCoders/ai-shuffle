// First launch and every restart select a locally built, authenticated source revision.
import { readFile, access, mkdir } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { verify } from 'node:crypto';
import { trustedKeys } from './trusted-keys.mjs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import net from 'node:net';
import { createInterface } from 'node:readline/promises';
import path from 'node:path';
const desktop = path.resolve(fileURLToPath(new URL('..', import.meta.url)));
const { readProfile, createProfile, localFeed } = createRequire(import.meta.url)('./source-profile.cjs');
const options = {}, launchArgs = [];
for (let i = 2; i < process.argv.length; i++) {
  const name = process.argv[i];
  if (name === '--isolated') options.isolated = true;
  else if (['--root', '--port', '--protected-port', '--debug-port', '--feed-directory', '--public-key'].includes(name)) {
    if (!process.argv[i + 1] || process.argv[i + 1].startsWith('--')) throw Error(`Missing value for ${name}`);
    options[name] = process.argv[++i];
  } else if (name.startsWith('--')) throw Error(`Unknown launcher option: ${name}`);
  else launchArgs.push(name);
}
const root = options['--root'] ? path.resolve(options['--root']) : path.join(process.env.LOCALAPPDATA, 'AI Shuffle Source');
let profile = readProfile(root);
if (options.isolated) {
  if (!options['--root']) throw Error('--isolated requires a separate --root');
  profile = createProfile(root, { port: Number(options['--port'] ?? 18080), protectedPort: Number(options['--protected-port'] ?? 18081), debugPort: options['--debug-port'] ? Number(options['--debug-port']) : undefined, feedDirectory: options['--feed-directory'], publicKeyFile: options['--public-key'] });
} else if (Object.keys(options).some(key => key !== '--root')) throw Error('Port, debug, and test-feed options require --isolated');
await mkdir(root, { recursive: true });
const publicKey = profile?.publicKey ?? await readFile(path.join(desktop, 'build/application-update-key.pem'), 'utf8');
async function askLicenseKey() {
  const prompt = createInterface({ input: process.stdin, output: process.stdout });
  try { return (await prompt.question('License key (ASH-XXXXX-XXXXX-XXXXX-XXXXX), or Enter to stop: ')).trim(); }
  finally { prompt.close(); }
}
async function exists(file) { try { await access(file); return true; } catch { return false; } }
async function run(exe, args, cwd, env = process.env, windowsHide = true) {
  await new Promise((resolve, reject) => {
    const child = spawn(exe, args, { cwd, env, stdio: 'inherit', windowsHide });
    child.once('error', reject);
    child.once('exit', (code, signal) => code === 0 ? resolve() : reject(new Error(`${path.basename(exe)} failed (${signal ?? code})`)));
  });
}
let state = {};
try { state = JSON.parse(await readFile(path.join(root, 'state.json'), 'utf8')); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
let implementation = desktop;
if (state.active && /^[a-f0-9]{40}$/.test(state.active)) {
  const dir = path.join(root, 'versions', state.active);
  const signed = JSON.parse(await readFile(path.join(dir, 'release.json'), 'utf8'));
  const payload = Buffer.from(signed.payload, 'base64');
  if (!trustedKeys(publicKey).some(key => verify(null, payload, key, Buffer.from(signed.signature, 'base64'))) || JSON.parse(payload).revision !== state.active) throw new Error('Installed source signature is invalid');
  implementation = path.join(dir, 'source/desktop');
}
const compiled = path.join(implementation, 'dist/main/source-updates.js');
if (!(await exists(compiled))) {
  console.log('Preparing the first-run launcher. No developer tools are required.');
  const npm = path.join(path.dirname(process.execPath), 'node_modules/npm/bin/npm-cli.js');
  const env = { ...process.env, ELECTRON_SKIP_BINARY_DOWNLOAD: '1', CI: 'true' };
  delete env.NODE_OPTIONS; delete env.PYTHONPATH;
  await run(process.execPath, [npm, 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], implementation, env);
  await run(process.execPath, [npm, 'run', 'build'], implementation, env);
}
const require = createRequire(path.join(implementation, 'package.json'));
const { SourceUpdates } = require(compiled);
const { buildEnvironment } = require(path.join(implementation, 'dist/main/source-build.js'));
const updates = new SourceUpdates({ root, publicKey, bootstrapNode: process.execPath, fetch: localFeed(profile) });
async function free(port) {
  return new Promise(resolve => { const socket = net.createServer(); socket.once('error', () => resolve(false)); socket.listen(port, '127.0.0.1', () => socket.close(() => resolve(true))); });
}
let selected;
if (!(await free(profile?.port ?? 8000)) || !(await free(profile?.protectedPort ?? 8001))) {
  if (!state.active) throw new Error('Close the existing AI Shuffle worker, then start the launcher again. No user data was changed.');
  selected = await updates.application(state.active);
} else {
  await updates.load();
  try { selected = await updates.select(); }
  catch {
    console.log('Preparing the latest authenticated source from main. The first build can take several minutes.');
    let lastMessage;
    // Older installed source managers acquired updates through check().
    const download = () => (updates.download ?? updates.check).call(updates, (percent, message) => { if (message !== lastMessage) { console.log(message); lastMessage = message; } }, true);
    // A first install has no worker and so no license lease. When the hub asks
    // for a license (402), collect the owner's key here, keep it beside the
    // update state, and retry: the hub checks it with the license server.
    for (let attempt = 0; ; attempt++) {
      try { await download(); break; }
      catch (error) {
        if (error?.code !== 'license_required' || !updates.saveLicenseKey || attempt >= 3 || !process.stdin.isTTY) throw error;
        console.log(error.message);
        const key = await askLicenseKey();
        if (!key) throw error;
        try { await updates.saveLicenseKey(key); } catch (invalid) { console.log(invalid.message); }
      }
    }
    await updates.prepareInstall?.();
    selected = await updates.select();
  }
}
console.log(`Starting AI Shuffle (${selected.revision.slice(0, 12)})`);
const env = { ...buildEnvironment(selected.node, selected.python), SHUFFLE_SOURCE_ROOT: root, SHUFFLE_SOURCE_REVISION: selected.revision, SHUFFLE_SOURCE_NODE: selected.node };
// Only build helpers use windowsHide; Electron must be allowed to show its GUI.
try { await run(selected.electron, [selected.desktopDir, ...(profile?.debugPort ? [`--remote-debugging-port=${profile.debugPort}`, '--remote-debugging-address=127.0.0.1'] : []), ...launchArgs], selected.desktopDir, env, false); }
catch (error) {
  // The Electron process owns health state. Its successful boot may already
  // have replaced this launcher's stale in-memory state; never overwrite it.
  throw error;
}
