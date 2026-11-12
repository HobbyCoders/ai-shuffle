// Fetch the Electron binary once, before `node --test` starts the test files in
// parallel processes (issue #1495). Electron 44 has no postinstall download:
// `require('electron')` downloads and extracts the binary when it is missing.
// Several test files require it at load, so after a fresh `npm ci` two of them
// extracted into node_modules/electron/dist at the same time. One then spawned
// a binary the other was still writing (ETXTBSY on Linux), or its extraction
// failed on the other's files and its real-Electron tests skipped.
//
// It never fails the run: with no download possible (offline), the test files
// still skip their real-Electron tests with "no Electron binary installed".
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
try {
  require('electron');
} catch (error) {
  console.warn(`Electron binary unavailable; the real-Electron tests will skip: ${error.message}`);
}
