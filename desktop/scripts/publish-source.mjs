// Publish a signed, runtime-only source revision for in-app updates.
// Archives live in a private GitHub release; signed metadata lives in the
// account hub's D1. No R2 and no application or installer build.
//
// SOURCE_CHANNEL=nightly (desktop-source.yml, daily after a local full pass):
//   current main, served at /v1/desktop/manifest/nightly and its /edge alias.
// The workflow keeps SOURCE_CHANNEL=edge for the first compatibility release.
// SOURCE_CHANNEL=stable (release.yml, after the release image passed container
//   verification): the commit of RELEASE_TAG, which must be on main. Served at
//   /v1/desktop/manifest, the address every customer install follows.
//
// The archive is scripts/source_archive.py's runtime-only tree, never GitHub's
// zipball of the whole repository. The signed payload keeps format 2 and every
// existing field, and adds channel, version, notes, tree and desktop.launcher.
import { sign, verify } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { readFile, writeFile, unlink } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { readLauncher } from './launcher-contract.mjs';
import { releaseRepository } from './release-repository.mjs';
const { repository: repo, id: repositoryId } = await releaseRepository(process.env.GH_TOKEN);
const channel = process.env.SOURCE_CHANNEL || 'edge';
if (!['stable', 'nightly', 'edge'].includes(channel)) throw new Error('SOURCE_CHANNEL must be stable or nightly (edge is a transition alias)');
// Both public aliases share the existing D1 lane; never relabel signed bytes.
const storageChannel = channel === 'nightly' ? 'edge' : channel;
const git = (...args) => execFileSync('git', args, { encoding: 'utf8' }).trim();
const releaseTag = process.env.RELEASE_TAG ?? '';
let revision, version, notes = '';
if (channel === 'stable') {
 if (!/^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/.test(releaseTag)) throw new Error('RELEASE_TAG must be a final vX.Y.Z for a stable publish');
 revision = git('rev-parse', `refs/tags/${releaseTag}^{commit}`);
 // Only a release cut from the protected branch is signed for customers.
 git('fetch', '--quiet', '--tags', 'origin', '+refs/heads/main:refs/remotes/origin/main');
 try { git('merge-base', '--is-ancestor', revision, 'refs/remotes/origin/main'); }
 catch { throw new Error(`${releaseTag} (${revision}) is not on main; refusing to sign it as a stable release`); }
 version = releaseTag.slice(1);
 // Release notes come from the annotated tag message (git tag -a vX.Y.Z -F notes.md).
 if (git('cat-file', '-t', `refs/tags/${releaseTag}`) === 'tag') {
  notes = git('tag', '-l', '--format=%(contents:body)', releaseTag) || git('tag', '-l', '--format=%(contents:subject)', releaseTag);
  notes = notes.replace(/-----BEGIN [A-Z ]*SIGNATURE-----[\s\S]*$/, '').trim();
 }
 if (notes.length > 4000) notes = `${notes.slice(0, 3990).trimEnd()}\n…`;
} else {
 revision = process.env.GITHUB_SHA;
 version = execFileSync('python', ['android/ci/resolve-version-name.py'], { encoding: 'utf8' }).trim();
}
if (!/^[a-f0-9]{40}$/.test(revision ?? '')) throw new Error('Invalid source revision');
// The runtime contract and this script are read from the checkout, so it must be the revision itself.
if (git('rev-parse', 'HEAD') !== revision) throw new Error(`Check out ${revision} before publishing it`);
if (!/^[0-9]+\.[0-9]+\.[0-9]+([.+-][A-Za-z0-9.+-]{1,60})?$/.test(version ?? '')) throw new Error(`Invalid release version ${version}`);
const key = process.env.DESKTOP_APPLICATION_SIGNING_KEY, token = process.env.GH_TOKEN;
const databaseId = process.env.ACCOUNT_HUB_DATABASE_ID;
if (!key || !token) throw new Error('Source publishing credentials are missing');
if (!/^[a-f0-9-]{36}$/.test(databaseId ?? '')) throw new Error('ACCOUNT_HUB_DATABASE_ID is missing or invalid');
const headers = { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'User-Agent': 'AI-Shuffle-Source-Publisher' };
async function api(route, options = {}) {
 return fetch(`https://api.github.com/repos/${repo}/${route}`, { ...options, headers: { ...headers, ...options.headers } });
}
// Nightly follows main: a superseded run publishes nothing. Stable is pinned to its tag.
async function current() {
 if (channel === 'stable') {
  // Ignore legacy milestone tags, and never make an older final look like a
  // new update merely because this signing job ran later (sequence is time).
  const parts = tag => tag.slice(1).split('.').map(BigInt);
  const wanted = parts(releaseTag);
  return git('tag', '--list').split('\n').filter(tag => /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/.test(tag)).every(tag => {
   const candidate = parts(tag);
   for (let i = 0; i < 3; i++) if (wanted[i] !== candidate[i]) return wanted[i] > candidate[i];
   return true;
  });
 }
 const r = await api('commits/main');
 if (!r.ok) throw new Error(`Main lookup failed (${r.status})`);
 return (await r.json()).sha === revision;
}
if (!(await current())) { console.log('A newer source is available; skipping obsolete source pointer.'); process.exit(0); }
let releaseResponse = await api('releases/tags/desktop-sources');
if (releaseResponse.status === 404) {
 releaseResponse = await api('releases', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
  tag_name: 'desktop-sources', target_commitish: revision, name: 'Desktop source archives', prerelease: true, make_latest: 'false',
  body: 'Runtime source archives for AI Shuffle in-app updates. The account hub serves them with signed update metadata. This is not an application release.',
 }) });
}
if (!releaseResponse.ok) throw new Error(`Source release lookup failed (${releaseResponse.status})`);
const release = await releaseResponse.json();
// A new name: assets published before this change are full-repository zipballs.
const assetName = `${revision}-runtime.zip`;
let asset;
for (let page = 1; ; page++) {
 const r = await api(`releases/${release.id}/assets?per_page=100&page=${page}`);
 if (!r.ok) throw new Error(`Source asset lookup failed (${r.status})`);
 const assets = await r.json();
 asset = assets.find(item => item.name === assetName);
 if (asset || assets.length < 100) break;
}
const archive = path.join(process.env.RUNNER_TEMP, assetName);
const configFile = path.join(process.env.RUNNER_TEMP, 'desktop-source-d1.json');
try {
 await writeFile(configFile, JSON.stringify({ d1_databases: [{ binding: 'DB', database_name: 'shuffle-account-hub', database_id: databaseId }] }));
 const quote = value => `'${value.replaceAll("'", "''")}'`;
 const env = { ...process.env }; delete env.DESKTOP_APPLICATION_SIGNING_KEY; delete env.GH_TOKEN;
 const wrangler = (...args) => execFileSync(process.execPath, ['scripts/source-publisher/node_modules/wrangler/bin/wrangler.js', 'd1', 'execute', 'DB', '--remote', '--config', configFile, ...args], { env, encoding: 'utf8' });
 // Several CI completions can authorize the same revision. Preserve its original
 // signed sequence rather than turning each completion into another update.
 const existing = JSON.parse(wrangler('--command', `SELECT manifest,asset_id,release_repository,release_repository_id FROM source_releases WHERE revision=${quote(revision)} AND channel=${quote(storageChannel)};`, '--json'));
 const published = existing.flatMap(item => item.results ?? [])[0];
 if (published) {
  const signed = JSON.parse(published.manifest);
  const payload = Buffer.from(signed.payload, 'base64');
  const manifest = JSON.parse(payload);
  if (published.release_repository !== repo || published.release_repository_id !== repositoryId ||
      manifest.revision !== revision || manifest.channel !== channel || published.asset_id !== asset?.id ||
      asset?.state !== 'uploaded' || manifest.size !== asset.size || asset.digest !== `sha256:${manifest.sha256}` ||
      !verify(null, payload, await readFile(new URL('../build/application-update-key.pem', import.meta.url), 'utf8'), Buffer.from(signed.signature, 'base64'))) {
   throw new Error('Existing source publication is invalid; refusing to overwrite it');
  }
  console.log(`Already published ${channel} ${revision}; leaving the signed update sequence unchanged.`);
 } else {
  // git archive is reproducible, so a retry rebuilds identical bytes and the
  // existing asset must match them exactly; bytes behind an asset ID never change.
  const built = JSON.parse(execFileSync('python', ['scripts/source_archive.py', revision, archive], { encoding: 'utf8' }));
  const { sha256, size, tree } = built;
  if (!/^[a-f0-9]{64}$/.test(sha256) || !/^[a-f0-9]{64}$/.test(tree) || !Number.isSafeInteger(size) || size <= 0 || size > 512 * 1024 ** 2) throw new Error('Invalid runtime source archive');
  const sequence = Date.now();
  const history = JSON.parse(wrangler('--command', 'SELECT MAX(sequence) AS maximum FROM source_releases;', '--json')).flatMap(item => item.results ?? [])[0];
  const inventory = JSON.parse(execFileSync('python', ['scripts/release/sequence_offset.py', 'signing-floor', '--revision', revision], { encoding: 'utf8' }));
  let installerFloor = Math.max(Number(inventory.floor), Number(git('show', '-s', '--format=%ct', revision)) * 1000);
  const installers = JSON.parse(wrangler('--command', 'SELECT revision,release_repository_id FROM desktop_installers UNION SELECT revision,release_repository_id FROM desktop_platform_installers;', '--json')).flatMap(item => item.results ?? []);
  for (const installer of installers) {
   if (installer.release_repository_id === inventory.old_repository_id && inventory.floor > 0) continue; // included in frozen historical inventory
   if (installer.release_repository_id !== repositoryId || !/^[a-f0-9]{40}$/.test(installer.revision)) throw Error('Unknown installer sequence provenance');
   installerFloor = Math.max(installerFloor, Number(git('show', '-s', '--format=%ct', installer.revision)) * 1000);
  }
  if (!history || !Number.isSafeInteger(sequence) || !Number.isSafeInteger(installerFloor) || sequence <= Number(history.maximum || 0) || sequence <= installerFloor) throw Error('Source signing clock did not exceed historical sequence/installer baseline');
  const containerSequence = Number(execFileSync('python', ['scripts/release/sequence_offset.py', 'show', '--revision', revision], { encoding: 'utf8' }).trim());
  if (!Number.isSafeInteger(containerSequence) || containerSequence <= 0) throw Error('Invalid committed container sequence');
  // What the revision needs from the Docker image (docker/update_contract.py,
  // #1821). runtime is all that images built before capabilities compare.
  const contract = JSON.parse(execFileSync('python', ['docker/update_contract.py', '--manifest'], { encoding: 'utf8' }));
  const { runtime, requires, recommends } = contract;
  const capabilities = list => Array.isArray(list) && list.length <= 32 && list.every(name => typeof name === 'string' && /^[a-z0-9][a-z0-9.-]{0,39}$/.test(name));
  if (!/^[a-f0-9]{64}$/.test(runtime) || !capabilities(requires) || !capabilities(recommends)) throw Error('Invalid container runtime contract');
  // The oldest Windows launcher (the code in an installed app.asar) this
  // revision runs under (desktop/launcher.json, #1821 D5). Clients before it ignore the field.
  const { minimum: launcher } = readLauncher(await readFile(new URL('../launcher.json', import.meta.url), 'utf8'));
  const payload = Buffer.from(JSON.stringify({ format: 2, revision, sequence, sha256, size, container: { sequence: containerSequence, runtime, requires, recommends },
   desktop: { launcher }, channel, version, notes, tree }));
  const signature = sign(null, payload, key);
  if (!verify(null, payload, await readFile(new URL('../build/application-update-key.pem', import.meta.url), 'utf8'), signature)) throw new Error('Signing key does not match launcher trust key');
  const signed = JSON.stringify({ payload: payload.toString('base64'), signature: signature.toString('base64') });
  if (signed.length > 32768) throw new Error('Signed manifest exceeds the 32 KiB client limit; shorten the release notes');
  if (!asset) {
   const uploaded = await fetch(`https://uploads.github.com/repos/${repo}/releases/${release.id}/assets?name=${assetName}`, {
    method: 'POST', headers: { ...headers, 'Content-Type': 'application/zip', 'Content-Length': String(size) },
    body: createReadStream(archive), duplex: 'half', signal: AbortSignal.timeout(120000),
   });
   if (!uploaded.ok) throw new Error(`Source upload failed (${uploaded.status})`);
   asset = await uploaded.json();
  }
  if (!Number.isSafeInteger(asset.id) || asset.id <= 0 || asset.state !== 'uploaded' || asset.size !== size || asset.digest !== `sha256:${sha256}`) throw new Error('GitHub source asset checksum or size did not match');
  if (!(await current())) { console.log('Source stored, but main moved ahead; pointer unchanged.'); }
  else {
   const insert = `INSERT INTO source_releases(revision,channel,version,asset_id,size,manifest,sequence,release_repository,release_repository_id) SELECT ${quote(revision)},${quote(storageChannel)},${quote(version)},${asset.id},${size},${quote(signed)},${sequence},${quote(repo)},${repositoryId} WHERE ${sequence} > COALESCE((SELECT MAX(sequence) FROM source_releases),0) ON CONFLICT(revision,channel) DO NOTHING;`;
   // Small queries use the query API. --file invokes the bulk import path,
   // which emits upload progress even with --json and can pause DB queries.
   wrangler('--command', insert);
   const result = JSON.parse(wrangler('--command', `SELECT manifest,asset_id,release_repository,release_repository_id FROM source_releases WHERE revision=${quote(revision)} AND channel=${quote(storageChannel)};`, '--json'));
   const row = result.flatMap(item => item.results ?? [])[0];
   if (row?.manifest !== signed || row?.asset_id !== asset.id || row?.release_repository !== repo || row?.release_repository_id !== repositoryId) throw new Error('Source pointer read-back did not match');
   console.log(`Published ${channel} ${version} (${revision}): ${size} runtime source bytes in GitHub asset ${asset.id}; signed D1 manifest verified.`);
  }
 }
} finally { for (const file of [archive, configFile]) await unlink(file).catch(() => {}); }
