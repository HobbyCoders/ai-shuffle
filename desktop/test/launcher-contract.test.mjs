import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { readLauncher } from '../scripts/launcher-contract.mjs';

const launcherJson = new URL('../launcher.json', import.meta.url);

test('#1821 D5: desktop/launcher.json holds a launcher version and a minimum no newer than it', () => {
 const { version, minimum } = readLauncher(readFileSync(launcherJson, 'utf8'));
 assert(version >= 1 && minimum >= 0 && minimum <= version);
 assert.deepEqual(readLauncher('{"version":2,"minimum":2}'), { version: 2, minimum: 2 });
 for (const bad of ['{}', 'null', '[]', '{"version":0,"minimum":0}', '{"version":1,"minimum":2}', '{"version":1.5,"minimum":0}',
  '{"version":"1","minimum":0}', '{"version":1,"minimum":-1}', '{"version":1}'])
  assert.throws(() => readLauncher(bad), /launcher\.json must hold/, bad);
});

test('#1821 D5: the installer stamp records the launcher version next to the baseline revision', () => {
 // Run the real stamp script with git and the output file intercepted.
 const preload = `
  import fs from 'node:fs'; import cp from 'node:child_process'; import { syncBuiltinESMExports } from 'node:module';
  cp.execFileSync = (exe, args) => args[0] === 'rev-parse' ? '${'a'.repeat(40)}\\n' : '1700000000\\n';
  fs.promises.writeFile = async (file, text) => { if (!String(file).endsWith('desktop-build.json')) throw Error('unexpected write ' + file); process.stdout.write('STAMP ' + text + '\\n'); };
  syncBuiltinESMExports();`;
 const result = spawnSync(process.execPath, ['--import', 'data:text/javascript,' + encodeURIComponent(preload), fileURLToPath(new URL('../scripts/stamp-desktop-build.mjs', import.meta.url))], { encoding: 'utf8' });
 assert.equal(result.status, 0, result.stderr);
 const stamp = JSON.parse(result.stdout.split('\n').find(line => line.startsWith('STAMP ')).slice(6));
 assert.deepEqual(stamp, { revision: 'a'.repeat(40), sequence: 1700000000000, launcher: readLauncher(readFileSync(launcherJson, 'utf8')).version });
});
