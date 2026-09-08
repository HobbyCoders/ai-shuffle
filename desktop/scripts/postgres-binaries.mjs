/**
 * Where the bundled PostgreSQL comes from: the pure part of the download,
 * kept apart from build-worker.mjs so it can be unit-tested without a build.
 *
 * theseus-rs/postgresql-binaries publishes relocatable PostgreSQL builds per
 * Rust target triple, each as `postgresql-<version>-<target>.tar.gz` with a
 * `.sha256` sidecar next to it.
 */

export const POSTGRES_VERSION = '16.10.0';
export const POSTGRES_RELEASES = 'https://github.com/theseus-rs/postgresql-binaries/releases/download';

export const POSTGRES_TARGETS = {
	'win32-x64': 'x86_64-pc-windows-msvc',
	'darwin-x64': 'x86_64-apple-darwin',
	'darwin-arm64': 'aarch64-apple-darwin',
	'linux-x64': 'x86_64-unknown-linux-gnu',
	'linux-arm64': 'aarch64-unknown-linux-gnu'
};

export function postgresTarget(platform, arch) {
	const target = POSTGRES_TARGETS[`${platform}-${arch}`];
	if (!target) throw new Error(`no PostgreSQL binaries for ${platform}-${arch}`);
	return target;
}

export function postgresArchiveName(version, target) {
	return `postgresql-${version}-${target}.tar.gz`;
}

export function postgresArchiveUrl(version, target) {
	return `${POSTGRES_RELEASES}/${version}/${postgresArchiveName(version, target)}`;
}

export function postgresChecksumUrl(version, target) {
	return `${postgresArchiveUrl(version, target)}.sha256`;
}

/** The sidecar is either a bare digest or `sha256sum` output (`<digest>  <file>`). */
export function parseSha256Sidecar(text, name) {
	const token = String(text ?? '').trim().split(/\s+/)[0] ?? '';
	if (!/^[0-9a-fA-F]{64}$/.test(token)) throw new Error(`no sha256 digest for ${name} in its .sha256 sidecar`);
	return token.toLowerCase();
}

/** The entry recorded in worker/manifest.json (null when the build skipped PostgreSQL). */
export function postgresManifestEntry(version, target) {
	return { version, target, source: 'theseus-rs/postgresql-binaries' };
}

/** The relative path of initdb inside an extracted distribution, the marker for its root. */
export function initdbRelativePath(platform) {
	return platform === 'win32' ? 'bin/initdb.exe' : 'bin/initdb';
}
