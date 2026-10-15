// No signer import or private-key access: invoke only the shared activation CAS.
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { publishSourcePointer } from '../../desktop/scripts/source-pointer.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const row = JSON.parse(input);
if (row.channel !== 'stable' || !Object.hasOwn(row, 'expectedRevision')) throw Error('Verified stable baseline required');
const database = process.env.ACCOUNT_HUB_DATABASE_ID;
if (!/^[a-f0-9-]{36}$/.test(database ?? '')) throw Error('D1 database configuration required');
const directory = await mkdtemp(path.join(os.tmpdir(), 'release-activation-'));
try {
 const config = path.join(directory, 'd1.json');
 await writeFile(config, JSON.stringify({ d1_databases: [{ binding: 'DB', database_name: 'release-metadata', database_id: database }] }));
 const env = { ...process.env };
 for (const key of ['DESKTOP_APPLICATION_SIGNING_KEY', 'GH_TOKEN', 'GOOGLE_PLAY_SERVICE_ACCOUNT_JSON']) delete env[key];
 const wranglerPath = fileURLToPath(new URL('../source-publisher/node_modules/wrangler/bin/wrangler.js', import.meta.url));
 const wrangler = (...args) => execFileSync(process.execPath, [wranglerPath,
  'd1', 'execute', 'DB', '--remote', '--config', config, ...args], { env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
 publishSourcePointer(wrangler, row);
} finally {
 await rm(directory, { recursive: true, force: true });
}
