// Isolate the real platform-installer publisher from network, git and Cloudflare in tests.
// PLATFORM_TEST (JSON) describes the world: the tag's commit, the release asset
// GitHub reports, and the version already published for the platform, if any.
import fs from 'node:fs';
import cp from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { generateKeyPairSync } from 'node:crypto';
import path from 'node:path';
const root = process.env.RUNNER_TEMP;
const world = JSON.parse(process.env.PLATFORM_TEST);
const pair = generateKeyPairSync('ed25519');
const publicKey = pair.publicKey.export({ type: 'spki', format: 'pem' });
process.env.DESKTOP_APPLICATION_SIGNING_KEY = pair.privateKey.export({ type: 'pkcs8', format: 'pem' });
fs.writeFileSync(path.join(root, 'public.pem'), publicKey);
const readFile = fs.promises.readFile;
fs.promises.readFile = async (file, ...args) => String(file).endsWith('application-update-key.pem') ? publicKey : readFile(file, ...args);
const events = [];
let saved;
cp.execFileSync = (exe, args) => {
  if (exe === 'git') {
    if (args[0] === 'rev-parse' && args[1] === 'refs/tags/v1.2.3^{commit}') return world.revision + '\n';
    if (args[0] === 'fetch') return '';
    if (args[0] === 'merge-base') { events.push('on-main'); return ''; }
    throw Error('Unexpected git command ' + args.join(' '));
  }
  if (!args.includes('d1') || !args.includes('--remote') || !args.includes('--command')) throw Error('Unexpected external command');
  const sql = args[args.indexOf('--command') + 1];
  if (sql.startsWith('INSERT')) {
    const match = sql.match(/VALUES\('([a-z]+)','([^']+)','([a-f0-9]{40})','([^']+)','([^']+)',(\d+),(\d+),'([a-f0-9]{64})','([^']+)','([^']+)'\)/);
    if (!match) throw Error('Invalid publisher SQL');
    saved = { platform: match[1], version: match[2], revision: match[3], arch: match[4], file_name: match[5], asset_id: Number(match[6]), size: Number(match[7]), sha256: match[8], payload: match[9], signature: match[10] };
    events.push('publish');
    fs.writeFileSync(path.join(root, 'record.json'), JSON.stringify(saved));
    return '[]';
  }
  if (sql.startsWith('SELECT version')) return JSON.stringify([{ results: world.oldVersion ? [{ version: world.oldVersion }] : [] }]);
  if (sql.startsWith('SELECT asset_id')) return JSON.stringify([{ results: saved ? [saved] : [] }]);
  throw Error('Unexpected SQL');
};
syncBuiltinESMExports();
globalThis.fetch = async url => {
  events.push(String(url));
  if (String(url) === 'https://api.github.com/repos/HobbyCoders/ai-shuffle/releases/tags/v1.2.3') return Response.json({ assets: [world.asset] });
  throw Error('Unexpected network request');
};
process.on('exit', () => fs.writeFileSync(path.join(root, 'events.json'), JSON.stringify(events)));
