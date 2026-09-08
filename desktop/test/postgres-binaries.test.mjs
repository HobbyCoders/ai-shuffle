import assert from 'node:assert/strict';
import { test } from 'node:test';
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

test('the manifest entry names version and target', () => {
	assert.deepEqual(postgresManifestEntry('16.10.0', 'x86_64-unknown-linux-gnu'), {
		version: '16.10.0',
		target: 'x86_64-unknown-linux-gnu',
		source: 'theseus-rs/postgresql-binaries'
	});
});
