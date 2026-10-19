/** Manual preview only: opens the compiled notice on synthetic state, never the installed app. */
import { spawn } from 'node:child_process';
import { promises as fs } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const require = createRequire(import.meta.url);
const desktop = fileURLToPath(new URL('..', import.meta.url));
const revision = 'b'.repeat(40);
export function previewOptions(args) {
 const { values } = parseArgs({ args, options: { phase: { type: 'string' }, root: { type: 'string' }, electron: { type: 'string' }, release: { type: 'string' } } });
 if (!['checking', 'starting'].includes(values.phase) || !values.root || !path.isAbsolute(values.root)) {
  throw Error('Usage: node desktop/scripts/preview-update-splash.mjs --phase checking|starting --root <new absolute temporary directory> [--electron <electron.exe>] [--release 1.0.2]');
 }
 return { ...values, electron: values.electron ?? require('electron'), release: values.release ?? require('../package.json').version };
}
export async function previewUpdateSplash(options, run = spawn) {
 if (!['checking', 'starting'].includes(options.phase) || !path.isAbsolute(options.root)) throw Error('Preview needs a phase and an absolute temporary root');
 const mainDir = path.join(desktop, 'dist', 'main');
 await fs.access(path.join(mainDir, 'update-splash-main.js')); // npm --prefix desktop run build first
 const { splashArgs } = require('../dist/main/update-splash.js');
 // Exclusive mkdir: never overwrite, read or delete an existing app/data directory.
 await fs.mkdir(options.root, { mode: 0o700 });
 try {
  const state = { schemaVersion: 1, active: 'a'.repeat(40), pending: revision, ...(options.phase === 'starting' ? { booting: revision } : {}) };
  await fs.writeFile(path.join(options.root, 'state.json'), JSON.stringify(state), { flag: 'wx', mode: 0o600 });
  const env = { ...process.env };
  for (const key of ['ELECTRON_RUN_AS_NODE', 'SHUFFLE_SOURCE_ROOT', 'SHUFFLE_SOURCE_REVISION', 'SHUFFLE_SOURCE_NODE']) delete env[key];
  const child = run(options.electron, splashArgs({ ...options, mainDir, revision, profile: path.join(options.root, 'profile'), launcher: null }), { cwd: desktop, env, stdio: 'inherit', windowsHide: false });
  const stop = () => child.kill();
  process.on('SIGINT', stop); process.on('SIGTERM', stop);
  try {
   return await new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('close', (code, signal) => resolve(code ?? (signal ? 1 : 0)));
   });
  } finally { process.off('SIGINT', stop); process.off('SIGTERM', stop); }
 } finally { await fs.rm(options.root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }); }
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
 try { process.exitCode = await previewUpdateSplash(previewOptions(process.argv.slice(2))); }
 catch (error) { console.error(error.message); process.exitCode = 1; }
}
