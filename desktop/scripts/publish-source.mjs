// Mirror the existing private GitHub source archive; never compile an app or installer here.
import { createHash, sign, verify } from 'node:crypto';
import { readFile, writeFile, open, unlink } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
const repo = 'HobbyCoders/ai-shuffle', revision = process.env.GITHUB_SHA;
if (!/^[a-f0-9]{40}$/.test(revision ?? '')) throw new Error('Invalid main revision');
const key = process.env.DESKTOP_APPLICATION_SIGNING_KEY, token = process.env.GH_TOKEN;
if (!key || !token) throw new Error('Source publishing credentials are missing');
const headers = { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'User-Agent': 'AI-Shuffle-Source-Publisher' };
async function stillMain() {
 const r = await fetch(`https://api.github.com/repos/${repo}/commits/main`, { headers });
 if (!r.ok) throw new Error(`Main lookup failed (${r.status})`);
 return (await r.json()).sha === revision;
}
if (!(await stillMain())) { console.log('Main moved ahead; skipping obsolete source pointer.'); process.exit(0); }
const response = await fetch(`https://api.github.com/repos/${repo}/zipball/${revision}`, { headers, signal: AbortSignal.timeout(120000) });
if (!response.ok || !response.body) throw new Error(`Source ZIP request failed (${response.status})`);
const archive = path.join(process.env.RUNNER_TEMP, `desktop-source-${revision}.zip`);
const signedFile = path.join(process.env.RUNNER_TEMP, 'desktop-source-latest.json');
const readback = path.join(process.env.RUNNER_TEMP, 'desktop-source-readback.json');
const file = await open(archive, 'wx'), hash = createHash('sha256'); let size = 0;
try { for await (const chunk of response.body) { size += chunk.length; if (size > 512 * 1024 ** 2) throw new Error('Source ZIP exceeds limit'); hash.update(chunk); await file.writeFile(chunk); } }
finally { await file.close(); }
const payload = Buffer.from(JSON.stringify({ format: 2, revision, sequence: Date.now(), sha256: hash.digest('hex'), size }));
const signature = sign(null, payload, key);
if (!verify(null, payload, await readFile(new URL('../build/application-update-key.pem', import.meta.url), 'utf8'), signature)) throw new Error('Signing key does not match launcher trust key');
const signed = JSON.stringify({ payload: payload.toString('base64'), signature: signature.toString('base64') });
await writeFile(signedFile, signed);
const env = { ...process.env }; delete env.DESKTOP_APPLICATION_SIGNING_KEY; delete env.GH_TOKEN;
function wrangler(...args) { execFileSync(process.execPath, ['android/hub/node_modules/wrangler/bin/wrangler.js', ...args], { env, stdio: 'inherit' }); }
try {
 wrangler('r2', 'object', 'put', `ai-shuffle-desktop-sources/sources/${revision}.zip`, '--file', archive, '--content-type', 'application/zip', '--remote');
 if (!(await stillMain())) console.log('Source stored, but main moved ahead; pointer unchanged.');
 else {
  wrangler('r2', 'object', 'put', 'ai-shuffle-desktop-sources/latest.json', '--file', signedFile, '--content-type', 'application/json', '--remote');
  wrangler('r2', 'object', 'get', 'ai-shuffle-desktop-sources/latest.json', '--file', readback, '--remote');
  if ((await readFile(readback, 'utf8')) !== signed) throw new Error('Source pointer read-back did not match');
  console.log(`Published ${revision}: ${size} source bytes; no app or installer build.`);
 }
} finally { for (const file of [archive, signedFile, readback]) await unlink(file).catch(() => {}); }
