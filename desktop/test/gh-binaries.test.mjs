import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { mkdtemp, mkdir, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { GH_ASSETS, GH_VERSION, ghAsset, installGh } from '../scripts/gh-binaries.mjs';
import { download, extract, findRoot } from '../scripts/build-worker.mjs';
import { validateBundledSymlinks } from '../scripts/bundled-symlinks.mjs';

async function scratch(t) {
	const dir = await mkdtemp(path.join(os.tmpdir(), 'shuffle-gh-test-'));
	t.after(() => rm(dir, { recursive: true, force: true }));
	return dir;
}
const digest = (data) => createHash('sha256').update(data).digest('hex');

test('pins cover exactly the five desktop targets, using upstream asset names', () => {
	assert.equal(GH_VERSION, '2.102.0');
	for (const [target, suffix] of Object.entries({
		'linux-x64': 'linux_amd64.tar.gz', 'linux-arm64': 'linux_arm64.tar.gz',
		'win32-x64': 'windows_amd64.zip', 'darwin-x64': 'macOS_amd64.zip', 'darwin-arm64': 'macOS_arm64.zip'
	})) {
		const [platform, arch] = target.split('-');
		const pin = ghAsset(platform, arch);
		assert.equal(pin.name, `gh_${GH_VERSION}_${suffix}`);
		assert.equal(pin.url, `https://github.com/cli/cli/releases/download/v${GH_VERSION}/${pin.name}`);
		assert.match(pin.sha256, /^[a-f0-9]{64}$/);
	}
	assert.equal(Object.keys(GH_ASSETS).length, 5);
	assert.throws(() => ghAsset('win32', 'arm64'), /no GitHub CLI binaries/);
});

test('download verifies fresh and cached bytes, refetches corrupt cache, rejects mismatch', async (t) => {
	const dir = await scratch(t);
	const archive = path.join(dir, 'fixture.tar.gz');
	const data = 'verified fixture bytes';
	let calls = 0;
	t.mock.method(globalThis, 'fetch', async () => { calls++; return new Response(data); });
	await download('https://fixture.invalid/archive', archive, digest(data));
	assert.equal(await readFile(archive, 'utf8'), data);
	await download('https://fixture.invalid/archive', archive, digest(data));
	assert.equal(calls, 1);
	await writeFile(archive, 'corrupt cache');
	await download('https://fixture.invalid/archive', archive, digest(data));
	assert.equal(calls, 2);
	await assert.rejects(download('https://fixture.invalid/archive', path.join(dir, 'bad'), '0'.repeat(64)), /checksum mismatch/);
	assert.deepEqual((await readdir(dir)).sort(), ['fixture.tar.gz']);
});

for (const target of Object.keys(GH_ASSETS)) {
	test(`install ${target}: pinned download, only executable/license, manifest and relocatable output`, async (t) => {
		const dir = await scratch(t);
		const [platform, arch] = target.split('-');
		const outDir = path.join(dir, 'worker');
		const cacheDir = path.join(dir, 'cache');
		const binary = platform === 'win32' ? 'gh.exe' : 'gh';
		const entry = await installGh({ platform, arch, outDir, cacheDir, findRoot,
			download: async (url, dest, sha256) => {
				const pin = ghAsset(platform, arch);
				assert.equal(url, pin.url);
				assert.equal(dest, path.join(cacheDir, pin.name));
				assert.equal(sha256, pin.sha256);
			},
			extract: async (_archive, staging, strip) => {
				assert.equal(strip, 0);
				// Windows archives can have no wrapping directory; tar/macOS wrap.
				const root = platform === 'win32' ? staging : path.join(staging, 'gh-release');
				await mkdir(path.join(root, 'bin'), { recursive: true });
				await writeFile(path.join(root, 'bin', binary), 'gh fixture');
				await writeFile(path.join(root, 'LICENSE'), 'MIT license fixture');
				await writeFile(path.join(root, 'README.md'), 'not shipped');
			}
		});
		assert.equal(await readFile(path.join(outDir, entry.binary), 'utf8'), 'gh fixture');
		assert.equal(await readFile(path.join(outDir, entry.license), 'utf8'), 'MIT license fixture');
		assert.deepEqual((await readdir(path.join(outDir, 'bin'))).sort(), [binary, 'gh-LICENSE'].sort());
		assert.equal(entry.version, GH_VERSION);
		assert.equal(entry.sha256, ghAsset(platform, arch).sha256);
		assert.equal(entry.archive, ghAsset(platform, arch).name);
		assert.equal(entry.source, 'cli/cli');
		assert.deepEqual(await readdir(cacheDir), []); // staging gone
		if (platform !== 'win32') assert.equal((await stat(path.join(outDir, entry.binary))).mode & 0o777, 0o755);
		await validateBundledSymlinks(outDir, platform);
	});
}

test('missing LICENSE fails installation and removes staging', async (t) => {
	const dir = await scratch(t);
	const cacheDir = path.join(dir, 'cache');
	await assert.rejects(installGh({ platform: 'linux', arch: 'x64', outDir: path.join(dir, 'worker'), cacheDir,
		download: async () => {}, findRoot,
		extract: async (_archive, staging) => {
			await mkdir(path.join(staging, 'bin'));
			await writeFile(path.join(staging, 'bin', 'gh'), 'fixture');
		}
	}), /ENOENT/);
	assert.deepEqual(await readdir(cacheDir), []);
});

test('stub with gh-only downloads nothing and writes only a stub manifest', async (t) => {
	const dir = await scratch(t);
	const outDir = path.join(dir, 'worker');
	const result = spawnSync(process.execPath, ['scripts/build-worker.mjs', '--stub', '--gh-only', '--out', outDir],
		{ cwd: path.resolve(import.meta.dirname, '..'), encoding: 'utf8' });
	assert.equal(result.status, 0, result.stderr);
	assert.deepEqual(await readdir(outDir), ['manifest.json']);
	assert.equal(JSON.parse(await readFile(path.join(outDir, 'manifest.json'), 'utf8')).stub, true);
});

test('real tar fixture runs through download, verification, extraction and install', { skip: process.platform === 'win32' }, async (t) => {
	const dir = await scratch(t);
	const fixture = path.join(dir, 'fixture');
	await mkdir(path.join(fixture, 'gh-release', 'bin'), { recursive: true });
	await writeFile(path.join(fixture, 'gh-release', 'bin', 'gh'), '#!/bin/sh\necho fixture gh\n');
	await writeFile(path.join(fixture, 'gh-release', 'LICENSE'), 'MIT');
	const archive = path.join(dir, 'fixture.tar.gz');
	const result = spawnSync('tar', ['-czf', archive, '-C', fixture, 'gh-release'], { encoding: 'utf8' });
	assert.equal(result.status, 0, result.stderr);
	const bytes = await readFile(archive);
	t.mock.method(globalThis, 'fetch', async () => new Response(bytes));
	const outDir = path.join(dir, 'worker');
	await installGh({ platform: 'linux', arch: 'x64', outDir, cacheDir: path.join(dir, 'cache'), extract, findRoot,
		download: (url, dest, pin) => {
			assert.equal(pin, ghAsset('linux', 'x64').sha256);
			return download(url, dest, digest(bytes)); // fixture digest, no network
		}
	});
	await validateBundledSymlinks(outDir);
	const cli = spawnSync(path.join(outDir, 'bin', 'gh'), ['--version'], { encoding: 'utf8' });
	assert.equal(cli.status, 0, cli.stderr);
	assert.equal(cli.stdout.trim(), 'fixture gh');
});
