// The installed Electron binary for tests, found without ever downloading it
// (issue #1495). Electron 44's own index.js downloads and extracts the binary
// when `require('electron')` finds it missing, and test files run in parallel
// processes, so two of them extracted into the same dist at once. Only
// fetch-electron.mjs, which `npm test` runs before the test files, downloads.
// This reads the same files index.js does: path.txt, which install.js writes
// only after a complete extraction, and the binary it names under dist (or
// under ELECTRON_OVERRIDE_DIST_PATH).
import { existsSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';

const require = createRequire(import.meta.url);

export const NO_ELECTRON = 'no Electron binary installed (run `node test/helpers/fetch-electron.mjs` or `npm test` in desktop/ to download it)';

function defaultPackageDir() {
  try { return path.dirname(require.resolve('electron/package.json')); } catch { return null; }
}

/** The path of the installed Electron executable, or null when it is not there. */
export function installedElectron(packageDir = defaultPackageDir(), env = process.env) {
  if (!packageDir) return null;
  let executable;
  try { executable = readFileSync(path.join(packageDir, 'path.txt'), 'utf8'); } catch { executable = ''; }
  const full = env.ELECTRON_OVERRIDE_DIST_PATH
    ? path.join(env.ELECTRON_OVERRIDE_DIST_PATH, executable || 'electron')
    : executable && path.join(packageDir, 'dist', executable);
  return full && existsSync(full) ? full : null;
}
