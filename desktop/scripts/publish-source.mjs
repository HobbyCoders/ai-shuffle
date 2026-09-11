// Store source ZIPs in a private GitHub release; keep signed metadata in existing D1.
// No R2 or application/installer build.
import { createHash, sign, verify } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { readFile, writeFile, open, unlink } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
const repo = 'HobbyCoders/ai-shuffle', revision = process.env.GITHUB_SHA;
if (!/^[a-f0-9]{40}$/.test(revision ?? '')) throw new Error('Invalid main revision');
const key = process.env.DESKTOP_APPLICATION_SIGNING_KEY, token = process.env.GH_TOKEN;
const databaseId = process.env.ACCOUNT_HUB_DATABASE_ID;
if (!key || !token) throw new Error('Source publishing credentials are missing');
if (!/^[a-f0-9-]{36}$/.test(databaseId ?? '')) throw new Error('ACCOUNT_HUB_DATABASE_ID is missing or invalid');
const headers = { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'User-Agent': 'AI-Shuffle-Source-Publisher' };
async function api(route, options = {}) {
 return fetch(`https://api.github.com/repos/${repo}/${route}`, { ...options, headers: { ...headers, ...options.headers } });
}
async function stillMain() {
 const r = await api('commits/main');
 if (!r.ok) throw new Error(`Main lookup failed (${r.status})`);
 return (await r.json()).sha === revision;
}
if (!(await stillMain())) { console.log('Main moved ahead; skipping obsolete source pointer.'); process.exit(0); }
let releaseResponse = await api('releases/tags/desktop-sources');
if (releaseResponse.status === 404) {
 releaseResponse = await api('releases', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
  tag_name: 'desktop-sources', target_commitish: revision, name: 'Desktop source archives', prerelease: true, make_latest: 'false',
  body: 'Source archives for AI Shuffle desktop updates. The account hub serves these published archives publicly with signed update metadata. This is not a stable application release.',
 }) });
}
if (!releaseResponse.ok) throw new Error(`Source release lookup failed (${releaseResponse.status})`);
const release = await releaseResponse.json();
const assetName = `${revision}.zip`;
let asset;
for (let page = 1; ; page++) {
 const r = await api(`releases/${release.id}/assets?per_page=100&page=${page}`);
 if (!r.ok) throw new Error(`Source asset lookup failed (${r.status})`);
 const assets = await r.json();
 asset = assets.find(item => item.name === assetName);
 if (asset || assets.length < 100) break;
}
// Reuse an already-published ZIP on retries; never replace bytes behind an asset ID.
const archive = path.join(process.env.RUNNER_TEMP, assetName);
const sqlFile = path.join(process.env.RUNNER_TEMP, 'desktop-source.sql');
const configFile = path.join(process.env.RUNNER_TEMP, 'desktop-source-d1.json');
const readbackFile = path.join(process.env.RUNNER_TEMP, 'desktop-source-readback.sql');
try {
 let size = 0;
 const hash = createHash('sha256');
 if (asset) {
  execFileSync('gh', ['release', 'download', 'desktop-sources', '--repo', repo, '--pattern', assetName, '--dir', process.env.RUNNER_TEMP], { stdio: 'inherit' });
  for await (const chunk of createReadStream(archive)) { size += chunk.length; hash.update(chunk); }
 } else {
  const response = await api(`zipball/${revision}`, { signal: AbortSignal.timeout(120000) });
  if (!response.ok || !response.body) throw new Error(`Source ZIP request failed (${response.status})`);
  const file = await open(archive, 'wx');
  try { for await (const chunk of response.body) { size += chunk.length; if (size > 512 * 1024 ** 2) throw new Error('Source ZIP exceeds limit'); hash.update(chunk); await file.writeFile(chunk); } }
  finally { await file.close(); }
 }
 if (!size || size > 512 * 1024 ** 2) throw new Error('Invalid source ZIP size');
 const sha256 = hash.digest('hex'), sequence = Date.now();
 const payload = Buffer.from(JSON.stringify({ format: 2, revision, sequence, sha256, size }));
 const signature = sign(null, payload, key);
 if (!verify(null, payload, await readFile(new URL('../build/application-update-key.pem', import.meta.url), 'utf8'), signature)) throw new Error('Signing key does not match launcher trust key');
 const signed = JSON.stringify({ payload: payload.toString('base64'), signature: signature.toString('base64') });
 if (!asset) {
  const uploaded = await fetch(`https://uploads.github.com/repos/${repo}/releases/${release.id}/assets?name=${assetName}`, {
   method: 'POST', headers: { ...headers, 'Content-Type': 'application/zip', 'Content-Length': String(size) },
   body: createReadStream(archive), duplex: 'half', signal: AbortSignal.timeout(120000),
  });
  if (!uploaded.ok) throw new Error(`Source upload failed (${uploaded.status})`);
  asset = await uploaded.json();
 }
 if (!Number.isSafeInteger(asset.id) || asset.id <= 0 || asset.state !== 'uploaded' || asset.size !== size || asset.digest !== `sha256:${sha256}`) throw new Error('GitHub source asset checksum or size did not match');
 if (!(await stillMain())) { console.log('Source stored, but main moved ahead; pointer unchanged.'); }
 else {
  await writeFile(configFile, JSON.stringify({ d1_databases: [{ binding: 'DB', database_name: 'shuffle-account-hub', database_id: databaseId }] }));
  const quote = value => `'${value.replaceAll("'", "''")}'`;
  await writeFile(sqlFile, `INSERT INTO desktop_source_releases(revision,asset_id,size,manifest,sequence) VALUES(${quote(revision)},${asset.id},${size},${quote(signed)},${sequence}) ON CONFLICT(revision) DO UPDATE SET manifest=excluded.manifest,sequence=excluded.sequence WHERE asset_id=excluded.asset_id AND size=excluded.size;`);
  const env = { ...process.env }; delete env.DESKTOP_APPLICATION_SIGNING_KEY; delete env.GH_TOKEN;
  const wrangler = (...args) => execFileSync(process.execPath, ['android/hub/node_modules/wrangler/bin/wrangler.js', 'd1', 'execute', 'DB', '--remote', '--config', configFile, ...args], { env, encoding: 'utf8' });
  wrangler('--file', sqlFile);
  await writeFile(readbackFile, `SELECT manifest,asset_id FROM desktop_source_releases WHERE revision=${quote(revision)};`);
  const result = JSON.parse(wrangler('--file', readbackFile, '--json'));
  const row = result.flatMap(item => item.results ?? [])[0];
  if (row?.manifest !== signed || row?.asset_id !== asset.id) throw new Error('Source pointer read-back did not match');
  console.log(`Published ${revision}: ${size} source bytes in GitHub asset ${asset.id}; signed D1 manifest verified.`);
 }
} finally { for (const file of [archive, sqlFile, configFile, readbackFile]) await unlink(file).catch(() => {}); }
