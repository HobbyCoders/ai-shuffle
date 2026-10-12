// Register a verified installer with the account hub: a macOS or Linux
// desktop build, or the Android release APK.
//
//   node desktop/scripts/publish-platform-installer.mjs <mac|linux|android> <artifact dir> <vX.Y.Z>
//
// <artifact dir> is the downloaded artifact of the platform's build of the tag:
//   mac      desktop-macos-artifact.yml (macos-desktop-<version>-<sha>)
//   linux    desktop-linux-artifact.yml (linux-desktop-<version>-<sha>)
//   android  android.yml called with the tag as release_tag (android-download)
// The installer (the .dmg, the .AppImage or the .apk) must already be uploaded
// to the GitHub release of that tag. This script checks the build provenance
// and checksum file, checks the uploaded asset has the same bytes, signs an
// installer payload with the application update key, and writes the
// platform's row in desktop_platform_installers, which /v1/desktop/installers
// lists and /v1/desktop/installer/<platform> serves. The Windows pointer
// (desktop_installers, publish-installer.mjs) is not touched.
//
// Environment: GH_TOKEN, DESKTOP_APPLICATION_SIGNING_KEY,
// ACCOUNT_HUB_DATABASE_ID, CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, RUNNER_TEMP.
import { createHash, sign, verify } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { readdir, readFile, writeFile, unlink } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import path from 'node:path';

import { releaseRepository } from './release-repository.mjs';
// provenance and sums are the build's own record of what it made
// (android/ci/prepare-apk-download.py writes the Android pair).
const PLATFORMS = {
 mac: { arch: 'arm64', extension: '.dmg', provenance: 'macos-build-provenance.json', sums: 'SHA256SUMS' },
 linux: { arch: 'x64', extension: '.AppImage', provenance: 'linux-build-provenance.json', sums: 'SHA256SUMS' },
 // One universal APK; its name carries no version, the provenance does.
 android: { arch: 'universal', extension: '.apk', provenance: 'android-build.json', sums: 'AI-Shuffle-Android.apk.sha256' },
};
const [platform, dir, tag] = process.argv.slice(2);
const target = PLATFORMS[platform];
if (!target || !dir || !/^v\d+\.\d+\.\d+$/.test(tag ?? '')) throw Error('Usage: publish-platform-installer.mjs <mac|linux|android> <artifact dir> <vX.Y.Z>');
const version = tag.slice(1);
const key = process.env.DESKTOP_APPLICATION_SIGNING_KEY, token = process.env.GH_TOKEN, database = process.env.ACCOUNT_HUB_DATABASE_ID;
if (!key || !token || !/^[a-f0-9-]{36}$/.test(database ?? '')) throw Error('Publisher configuration missing');

// Only a stable tag on the protected branch is published, like the source feed.
const git = (...args) => execFileSync('git', args, { encoding: 'utf8' }).trim();
const revision = git('rev-parse', `refs/tags/${tag}^{commit}`);
git('fetch', '--quiet', 'origin', '+refs/heads/main:refs/remotes/origin/main');
try { git('merge-base', '--is-ancestor', revision, 'refs/remotes/origin/main'); }
catch { throw Error(`${tag} (${revision}) is not on main; refusing to publish its installer`); }

// The artifact must be the build of this tag's commit, at this version.
const provenance = JSON.parse(await readFile(path.join(dir, target.provenance), 'utf8'));
if (platform === 'android') {
 // android-build.json: Gradle's versionName and the commit Android CI checked out.
 if (provenance.versionName !== version) throw Error(`APK versionName ${provenance.versionName} is not ${version}; build it with ${tag} as the release tag`);
 if (provenance.commit !== revision) throw Error(`APK was built from ${provenance.commit}, not ${tag} (${revision})`);
} else {
 if (provenance.version !== version) throw Error(`Artifact version ${provenance.version} is not ${version}`);
 if (provenance.sourceCommit !== revision) throw Error(`Artifact was built from ${provenance.sourceCommit}, not ${tag} (${revision})`);
}
// D12: desktop sells signed. A Mac build must be Developer ID signed and notarized.
if (platform === 'mac' && !/^developer-id\+notary/.test(provenance.signing ?? '')) throw Error(`Mac artifact signing is "${provenance.signing}"; a notarized Developer ID build is required`);

const candidates = (await readdir(dir)).filter(name => name.endsWith(target.extension));
if (candidates.length !== 1) throw Error(`Expected exactly one ${target.extension} in ${dir}, found ${candidates.length}`);
const file = candidates[0];
if (!/^[A-Za-z0-9._-]+$/.test(file) || (platform !== 'android' && !file.includes(version))) throw Error(`Unexpected installer name ${file}`);
let size = 0; const hash = createHash('sha256');
for await (const chunk of createReadStream(path.join(dir, file))) { size += chunk.length; hash.update(chunk); }
const sha256 = hash.digest('hex');
const recorded = platform === 'android'
 ? { name: file, bytes: size, sha256: provenance.sha256 } // android-build.json records the digest only
 : provenance.artifacts?.find(a => a.name === file);
if (recorded?.bytes !== size || recorded?.sha256 !== sha256) throw Error('Installer does not match its build provenance');
const sums = await readFile(path.join(dir, target.sums), 'utf8');
if (!sums.split('\n').includes(`${sha256}  ${file}`)) throw Error(`Installer does not match ${target.sums}`);

const { repository: repo, id: repositoryId } = await releaseRepository(token);
const response = await fetch(`https://api.github.com/repos/${repo}/releases/tags/${tag}`, { redirect: 'manual', headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'User-Agent': 'AI-Shuffle-Installer-Publisher' } });
if (!response.ok) throw Error(`Release lookup failed (${response.status})`);
const asset = (await response.json()).assets.find(a => a.name === file);
if (!asset || asset.state !== 'uploaded' || asset.size !== size || asset.digest !== `sha256:${sha256}`) throw Error('Uploaded installer did not match verified bytes');

// The signed payload is what clients trust; the hub also lists its fields.
const payload = Buffer.from(JSON.stringify({ type: 'desktop-installer', format: 1, platform, arch: target.arch, version, revision, file, size, sha256 }));
const signature = sign(null, payload, key);
if (!verify(null, payload, await readFile(new URL('../build/application-update-key.pem', import.meta.url), 'utf8'), signature)) throw Error('Signing key does not match the desktop trust key');

const config = path.join(process.env.RUNNER_TEMP, `desktop-${platform}-installer-d1.json`);
try {
 await writeFile(config, JSON.stringify({ d1_databases: [{ binding: 'DB', database_name: 'shuffle-account-hub', database_id: database }] }));
 const env = { ...process.env }; delete env.DESKTOP_APPLICATION_SIGNING_KEY; delete env.GH_TOKEN;
 const command = sql => JSON.parse(execFileSync(process.execPath, ['scripts/source-publisher/node_modules/wrangler/bin/wrangler.js', 'd1', 'execute', 'DB', '--remote', '--config', config, '--command', sql, '--json'], { env, encoding: 'utf8' }));
 const quote = value => `'${String(value).replaceAll("'", "''")}'`;
 const old = command(`SELECT version,asset_id,release_repository,release_repository_id FROM desktop_platform_installers WHERE platform=${quote(platform)};`).flatMap(r => r.results ?? [])[0];
 if (old) {
  if (old.version === version && (old.asset_id !== asset.id || old.release_repository !== repo || old.release_repository_id !== repositoryId)) throw Error('Existing installer publication identity changed');
  const a = version.split('.').map(Number), b = old.version.split('.').map(Number), first = a.findIndex((n, i) => n !== b[i]);
  if (first >= 0 && a[first] < b[first]) throw Error(`Refusing to replace the newer ${platform} installer ${old.version}`);
 }
 const signed = { payload: payload.toString('base64'), signature: signature.toString('base64') };
 command(`INSERT INTO desktop_platform_installers(platform,version,revision,arch,file_name,asset_id,size,sha256,payload,signature,release_repository,release_repository_id) VALUES(${[platform, version, revision, target.arch, file].map(quote).join(',')},${asset.id},${size},${quote(sha256)},${quote(signed.payload)},${quote(signed.signature)},${quote(repo)},${repositoryId}) ON CONFLICT(platform) DO UPDATE SET version=excluded.version,revision=excluded.revision,arch=excluded.arch,file_name=excluded.file_name,asset_id=excluded.asset_id,size=excluded.size,sha256=excluded.sha256,payload=excluded.payload,signature=excluded.signature,release_repository=excluded.release_repository,release_repository_id=excluded.release_repository_id;`);
 const saved = command(`SELECT asset_id,sha256,payload,signature,release_repository,release_repository_id FROM desktop_platform_installers WHERE platform=${quote(platform)};`).flatMap(r => r.results ?? [])[0];
 if (saved?.asset_id !== asset.id || saved?.sha256 !== sha256 || saved?.payload !== signed.payload || saved?.signature !== signed.signature || saved?.release_repository !== repo || saved?.release_repository_id !== repositoryId) throw Error('Installer pointer verification failed');
 console.log(`Published ${platform} ${version}: ${file}, ${size} verified bytes, revision ${revision}`);
} finally { await unlink(config).catch(() => {}); }
