import assert from 'node:assert/strict';
import { test } from 'node:test';
import { spawnSync } from 'node:child_process';
import { cp, mkdir, mkdtemp, readlink, rm, symlink, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {
	POSTGRES_VERSION,
	POSTGRES_TARGETS,
	initdbRelativePath,
	parseSha256Sidecar,
	postgresArchiveName,
	postgresArchiveUrl,
	postgresChecksumUrl,
	postgresManifestEntry,
	postgresTarget
} from '../scripts/postgres-binaries.mjs';
import { validateBundledSymlinks } from '../scripts/bundled-symlinks.mjs';

async function postgresFixture(t) {
	const scratch = await mkdtemp(path.join(os.tmpdir(), 'shuffle-postgres-links-'));
	t.after(() => rm(scratch, { recursive: true, force: true }));
	const dir = path.join(scratch, 'postgres');
	await mkdir(path.join(dir, 'lib'), { recursive: true });
	return dir;
}

const symlinkTest = { skip: process.platform === 'win32' };

test('relative PostgreSQL links remain valid after copying and removing staging', symlinkTest, async (t) => {
	const dir = await postgresFixture(t);
	await writeFile(path.join(dir, 'lib', 'libpq.so.5.16'), 'library');
	await symlink('libpq.so.5.16', path.join(dir, 'lib', 'libpq.so.5'));
	await symlink('libpq.so.5', path.join(dir, 'lib', 'libpq.so'));
	await symlink('libpq.so.5.16', path.join(dir, 'lib', 'libpq.5.dylib'));
	await mkdir(path.join(dir, 'bin'));
	await symlink('../lib/libpq.5.dylib', path.join(dir, 'bin', 'client-library'));
	const copied = path.join(path.dirname(dir), 'copied');
	await cp(dir, copied, { recursive: true, verbatimSymlinks: true });
	await rm(dir, { recursive: true });
	assert.equal(await readlink(path.join(copied, 'lib', 'libpq.so.5')), 'libpq.so.5.16');
	await validateBundledSymlinks(copied);
});

test('absolute PostgreSQL links are rejected even when their target is inside', symlinkTest, async (t) => {
	const dir = await postgresFixture(t);
	const library = path.join(dir, 'lib', 'libpq.so.5.16');
	await writeFile(library, 'library');
	await symlink(library, path.join(dir, 'lib', 'libpq.so.5'));
	await assert.rejects(validateBundledSymlinks(dir), /Bundled symlink is absolute:.*libpq\.so\.5/);
});

test('relative PostgreSQL links escaping to a sibling directory are rejected', symlinkTest, async (t) => {
	const dir = await postgresFixture(t);
	const sibling = path.join(path.dirname(dir), 'postgres-outside');
	await mkdir(sibling);
	await writeFile(path.join(sibling, 'library'), 'library');
	await symlink('../../postgres-outside/library', path.join(dir, 'lib', 'libpq.so.5'));
	await assert.rejects(validateBundledSymlinks(dir), /Bundled symlink resolves outside resource directory/);
});

test('relative PostgreSQL links escaping through a directory link are rejected', symlinkTest, async (t) => {
	const dir = await postgresFixture(t);
	await writeFile(path.join(path.dirname(dir), 'library'), 'library');
	await symlink('..', path.join(dir, 'z-bridge'), 'dir');
	await symlink('../z-bridge/library', path.join(dir, 'lib', 'libpq.so.5'));
	await assert.rejects(validateBundledSymlinks(dir), /Bundled symlink resolves outside resource directory:.*libpq\.so\.5/);
});

test('dangling relative PostgreSQL links are rejected', symlinkTest, async (t) => {
	const dir = await postgresFixture(t);
	await symlink('missing-library', path.join(dir, 'lib', 'libpq.so.5'));
	await assert.rejects(validateBundledSymlinks(dir), { code: 'ENOENT' });
});

test('nested npm bin links work after staging removal and the old copy is rejected', symlinkTest, async (t) => {
	const scratch = path.dirname(await postgresFixture(t));
	const staging = path.join(scratch, 'staging');
	const modules = path.join(staging, 'server', 'browser-mcp', 'node_modules');
	await mkdir(path.join(modules, '.bin'), { recursive: true });
	const links = {
		'node-which': '../which/bin/node-which',
		'playwright-core': '../playwright-core/cli.js'
	};
	for (const [name, target] of Object.entries(links)) {
		const script = path.resolve(modules, '.bin', target);
		await mkdir(path.dirname(script), { recursive: true });
		await writeFile(script, `console.log('${name} works');\n`);
		await symlink(target, path.join(modules, '.bin', name));
	}
	const copied = path.join(scratch, 'worker');
	const broken = path.join(scratch, 'broken-worker');
	await cp(staging, copied, { recursive: true, verbatimSymlinks: true });
	await cp(staging, broken, { recursive: true });
	await rm(staging, { recursive: true });
	await assert.rejects(validateBundledSymlinks(broken), /Bundled symlink is absolute/);
	await validateBundledSymlinks(copied);
	for (const [name, target] of Object.entries(links)) {
		const link = path.join(copied, 'server', 'browser-mcp', 'node_modules', '.bin', name);
		assert.equal(await readlink(link), target);
		const result = spawnSync(process.execPath, [link], { encoding: 'utf8' });
		assert.equal(result.status, 0, result.stderr);
		assert.equal(result.stdout.trim(), `${name} works`);
	}
});

test('Windows skips bundled symlink validation', async () => {
	await validateBundledSymlinks('/does-not-exist', 'win32');
});

test('the pinned PostgreSQL is a concrete 16.x release', () => {
	assert.match(POSTGRES_VERSION, /^16\.\d+\.\d+$/);
});

test('every supported platform maps to a theseus target triple', () => {
	assert.equal(postgresTarget('win32', 'x64'), 'x86_64-pc-windows-msvc');
	assert.equal(postgresTarget('darwin', 'arm64'), 'aarch64-apple-darwin');
	assert.equal(postgresTarget('linux', 'x64'), 'x86_64-unknown-linux-gnu');
	assert.equal(postgresTarget('linux', 'arm64'), 'aarch64-unknown-linux-gnu');
	assert.throws(() => postgresTarget('win32', 'arm64'), /no PostgreSQL binaries for win32-arm64/);
	assert.deepEqual(Object.keys(POSTGRES_TARGETS).sort(), ['darwin-arm64', 'darwin-x64', 'linux-arm64', 'linux-x64', 'win32-x64']);
});

test('archive and checksum URLs follow the release layout', () => {
	assert.equal(postgresArchiveName('16.10.0', 'x86_64-unknown-linux-gnu'), 'postgresql-16.10.0-x86_64-unknown-linux-gnu.tar.gz');
	assert.equal(
		postgresArchiveUrl('16.10.0', 'aarch64-apple-darwin'),
		'https://github.com/theseus-rs/postgresql-binaries/releases/download/16.10.0/postgresql-16.10.0-aarch64-apple-darwin.tar.gz'
	);
	assert.equal(postgresChecksumUrl('16.10.0', 'x86_64-pc-windows-msvc'), `${postgresArchiveUrl('16.10.0', 'x86_64-pc-windows-msvc')}.sha256`);
	assert.equal(initdbRelativePath('win32'), 'bin/initdb.exe');
	assert.equal(initdbRelativePath('linux'), 'bin/initdb');
});

test('the sha256 sidecar is read whether bare or in sha256sum form', () => {
	const digest = 'a'.repeat(64);
	assert.equal(parseSha256Sidecar(`${digest}\n`, 'x'), digest);
	assert.equal(parseSha256Sidecar(`${digest.toUpperCase()}  postgresql-16.10.0-x.tar.gz\n`, 'x'), digest);
	assert.throws(() => parseSha256Sidecar('not a digest', 'x'), /no sha256 digest for x/);
	assert.throws(() => parseSha256Sidecar('', 'x'), /no sha256 digest/);
});

test('the Windows release CertUtil sidecar is parsed without bypassing SHA256', () => {
	const name = 'postgresql-16.10.0-x86_64-pc-windows-msvc.tar.gz';
	const digest = 'c49ab81118d409a02e77b131de7d78a648da31ce159d0cca59298a80468f857d';
	const sidecar = `SHA256 hash of ${name}:\r\n${digest}\r\nCertUtil: -hashfile command completed successfully.\r\n`;
	assert.equal(parseSha256Sidecar(sidecar, name), digest);
	for (const invalid of [
		sidecar.replace(name, 'another-file.tar.gz'),
		sidecar.replace('SHA256 hash', 'MD5 hash'),
		sidecar.replace(digest, digest.slice(1)),
		sidecar.replace(digest, `${digest}\r\n${digest}`),
		sidecar.replace('completed successfully.', 'failed.')
	]) assert.throws(() => parseSha256Sidecar(invalid, name), /no sha256 digest/);
});

test('the manifest entry names version and target', () => {
	assert.deepEqual(postgresManifestEntry('16.10.0', 'x86_64-unknown-linux-gnu'), {
		version: '16.10.0',
		target: 'x86_64-unknown-linux-gnu',
		source: 'theseus-rs/postgresql-binaries'
	});
});
