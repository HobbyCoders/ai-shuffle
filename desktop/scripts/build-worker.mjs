#!/usr/bin/env node
/**
 * Assemble the worker runtime the desktop app ships under resources/worker:
 *
 *   worker/
 *     manifest.json        versions and build time
 *     python/              CPython (python-build-standalone) with the server's
 *                          Python dependencies installed into its site-packages
 *     node/                Node.js for the AI tools and browser MCP packages
 *     bin/                 pinned GitHub CLI (gh) and gh-LICENSE
 *     server/
 *       app/               backend source, with the built frontend in app/static
 *       tools/             built @ai-shuffle/tools with production node_modules
 *       browser-mcp/       built browser MCP server with production node_modules
 *       downloads/         pinned, verified cloudflared for remote access
 *
 * PostgreSQL ships next to it, under resources/postgres (assembled here into
 * desktop/postgres/ from a theseus-rs/postgresql-binaries release):
 *
 *   postgres/
 *     bin/                 initdb, pg_ctl, postgres and the client tools
 *     lib/, share/         what those binaries need at run time
 *     manifest.json        version and target
 *
 * Usage:
 *   node scripts/build-worker.mjs [--out worker] [--platform win32|darwin|linux]
 *                                 [--arch x64|arm64] [--frontend-build <dir>]
 *                                 [--keep-python] [--stub] [--skip-postgres]
 *                                 [--postgres-out postgres] [--cache <dir>] [--runtime-only]
 *                                 [--postgres-only] [--gh-only]
 *
 * --runtime-only downloads the native runtimes locally, keeps npm for source
 * builds, and skips application/frontend/tool assembly. --cache reuses verified
 * downloads across runtime versions. No Windows installer is produced.
 *
 * --postgres-only assembles and smoke-tests just PostgreSQL (no worker runtime).
 * --gh-only assembles and smoke-tests just gh and writes its manifest entry.
 *
 * --stub writes only a manifest so electron-builder can package a shell without
 * a runtime (used by pull-request CI on platforms that do not build one).
 * --skip-postgres leaves PostgreSQL out (a development build that uses
 * SHUFFLE_DESKTOP_POSTGRES_DIR); the manifest says so.
 */
import { createHash } from 'node:crypto';
import { createWriteStream, existsSync } from 'node:fs';
import { cp, mkdir, mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { pipeline } from 'node:stream/promises';
import { Readable } from 'node:stream';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { runtimeContract, pythonDependencies } from './runtime-contract.mjs';
import {
	POSTGRES_VERSION,
	initdbRelativePath,
	parseSha256Sidecar,
	postgresArchiveName,
	postgresArchiveUrl,
	postgresChecksumUrl,
	postgresManifestEntry,
	postgresTarget
} from './postgres-binaries.mjs';
import { validateBundledSymlinks } from './bundled-symlinks.mjs';
import { installGh } from './gh-binaries.mjs';

const PYTHON_VERSION = '3.12.14';
const PYTHON_BUILD = '20260901';
const NODE_VERSION = '22.23.2';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.resolve(here, '..');
const repoDir = path.resolve(desktopDir, '..');
const args = parseArgs(process.argv.slice(2));
const cacheDir = path.resolve(args.cache ?? path.join(desktopDir, '.cache'));
const platform = args.platform ?? process.platform;
const arch = args.arch ?? process.arch;
const outDir = path.resolve(desktopDir, args.out ?? 'worker');
const postgresDir = path.resolve(desktopDir, args['postgres-out'] ?? 'postgres');
const frontendBuild = args['frontend-build'] ? path.resolve(args['frontend-build']) : path.join(repoDir, 'frontend', 'build');

const PYTHON_TRIPLES = {
	'win32-x64': 'x86_64-pc-windows-msvc',
	'darwin-x64': 'x86_64-apple-darwin',
	'darwin-arm64': 'aarch64-apple-darwin',
	'linux-x64': 'x86_64-unknown-linux-gnu',
	'linux-arm64': 'aarch64-unknown-linux-gnu'
};
const NODE_NAMES = {
	'win32-x64': `node-v${NODE_VERSION}-win-x64.zip`,
	'win32-arm64': `node-v${NODE_VERSION}-win-arm64.zip`,
	'darwin-x64': `node-v${NODE_VERSION}-darwin-x64.tar.gz`,
	'darwin-arm64': `node-v${NODE_VERSION}-darwin-arm64.tar.gz`,
	'linux-x64': `node-v${NODE_VERSION}-linux-x64.tar.gz`,
	'linux-arm64': `node-v${NODE_VERSION}-linux-arm64.tar.gz`
};

function parseArgs(argv) {
	const out = {};
	for (let i = 0; i < argv.length; i++) {
		const a = argv[i];
		if (!a.startsWith('--')) continue;
		const key = a.slice(2);
		const next = argv[i + 1];
		if (next && !next.startsWith('--')) {
			out[key] = next;
			i++;
		} else {
			out[key] = true;
		}
	}
	return out;
}

function log(msg) {
	console.log(`[build-worker] ${msg}`);
}

function run(cmd, cmdArgs, opts = {}) {
	log(`$ ${cmd} ${cmdArgs.join(' ')}${opts.cwd ? `  (in ${path.relative(repoDir, opts.cwd) || '.'})` : ''}`);
	const result = spawnSync(cmd, cmdArgs, { stdio: 'inherit', shell: false, windowsHide: true, ...opts });
	if (result.status !== 0) {
		throw new Error(`${cmd} ${cmdArgs.join(' ')} exited with ${result.status ?? result.signal}`);
	}
}

export async function download(url, dest, expectedSha256) {
	if (existsSync(dest)) {
		if (!expectedSha256 || (await sha256(dest)) === expectedSha256) {
			log(`cached ${path.basename(dest)}`);
			return;
		}
		await rm(dest);
	}
	log(`downloading ${url}`);
	const res = await fetch(url, { redirect: 'follow' });
	if (!res.ok || !res.body) throw new Error(`download failed ${res.status} ${url}`);
	await mkdir(path.dirname(dest), { recursive: true });
	const tmp = `${dest}.part`;
	await pipeline(Readable.fromWeb(res.body), createWriteStream(tmp));
	if (expectedSha256) {
		const actual = await sha256(tmp);
		if (actual !== expectedSha256) {
			await rm(tmp);
			throw new Error(`checksum mismatch for ${path.basename(dest)}: ${actual} != ${expectedSha256}`);
		}
	}
	await rm(dest, { force: true });
	await cp(tmp, dest);
	await rm(tmp);
}

async function sha256(file) {
	const hash = createHash('sha256');
	hash.update(await readFile(file));
	return hash.digest('hex');
}

async function fetchText(url) {
	const res = await fetch(url, { redirect: 'follow' });
	if (!res.ok) throw new Error(`fetch failed ${res.status} ${url}`);
	return res.text();
}

/**
 * Extract an archive with the system tar (bsdtar on Windows handles zip too).
 * Paths are made relative to the destination because bsdtar reads a Windows
 * drive letter (`D:\...`) as a remote host.
 */
export async function extract(archive, dest, stripComponents = 1) {
	await rm(dest, { recursive: true, force: true });
	await mkdir(dest, { recursive: true });
	if (archive.endsWith('.zip') && process.platform === 'win32') {
		// GNU tar (Git for Windows) cannot read zip files; PowerShell always can.
		const staging = `${dest}.unzip`;
		await rm(staging, { recursive: true, force: true });
		run('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', `Expand-Archive -LiteralPath '${archive}' -DestinationPath '${staging}' -Force`]);
		const { readdir } = await import('node:fs/promises');
		let root = staging;
		for (let i = 0; i < stripComponents; i++) {
			const entries = await readdir(root);
			if (entries.length !== 1) throw new Error(`cannot strip ${stripComponents} components from ${archive}`);
			root = path.join(root, entries[0]);
		}
		await rm(dest, { recursive: true, force: true });
		await cp(root, dest, { recursive: true });
		await rm(staging, { recursive: true, force: true });
		return;
	}
	const rel = path.relative(dest, archive).split(path.sep).join('/');
	run('tar', ['-xf', rel, `--strip-components=${stripComponents}`], { cwd: dest });
}

function pythonExe(dir) {
	return platform === 'win32' ? path.join(dir, 'python.exe') : path.join(dir, 'bin', 'python3');
}

async function installPython() {
	const triple = PYTHON_TRIPLES[`${platform}-${arch}`];
	if (!triple) throw new Error(`no python-build-standalone build for ${platform}-${arch}`);
	if (args['keep-python'] && existsSync(pythonExe(path.join(outDir, 'python')))) {
		log('keeping the existing python/ directory (--keep-python)');
		return { version: PYTHON_VERSION, build: PYTHON_BUILD, triple, kept: true };
	}
	const asset = `cpython-${PYTHON_VERSION}+${PYTHON_BUILD}-${triple}-install_only.tar.gz`;
	const base = `https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_BUILD}/`;
	const sums = await fetchText(`${base}SHA256SUMS`);
	const line = sums.split('\n').find((l) => l.trim().endsWith(`  ${asset}`) || l.trim().endsWith(` ${asset}`));
	if (!line) throw new Error(`no checksum for ${asset} in SHA256SUMS`);
	const expected = line.trim().split(/\s+/)[0];
	const archive = path.join(cacheDir, asset);
	await download(`${base}${asset}`, archive, expected);
	const dir = path.join(outDir, 'python');
	await extract(archive, dir, 1);
	const py = pythonExe(dir);
	run(py, ['--version']);

	const env = { ...process.env, PIP_DISABLE_PIP_VERSION_CHECK: '1', PYTHONNOUSERSITE: '1' };
	run(py, ['-m', 'pip', 'install', '--no-cache-dir', '--upgrade', 'pip==26.2.1'], { env });
	// Hash-pinned, the same way the container image and updater install: every
	// package, including fluid's build backend, must match a hash in the locks.
	const pip = ['-m', 'pip', 'install', '--no-cache-dir', '--prefer-binary', '--timeout', '120', '--retries', '10'];
	run(py, [...pip, '--require-hashes', '-r', path.join(repoDir, 'requirements.lock')], { env, cwd: repoDir });
	run(py, [...pip, '--require-hashes', '-r', path.join(repoDir, 'requirements-native.lock')], { env, cwd: repoDir });
	run(py, [...pip, '--no-deps', '--no-build-isolation', path.join(repoDir, 'fluid')], { env, cwd: repoDir });
	run(py, ['-c', 'import importlib.util; import fluid, mcp, psycopg2, pygit2, fastapi; assert importlib.util.find_spec("claude_agent_sdk") is None; print("python deps ok (no agent SDK)")'], { env });
	return { version: PYTHON_VERSION, build: PYTHON_BUILD, triple };
}

async function installNode() {
	const name = NODE_NAMES[`${platform}-${arch}`];
	if (!name) throw new Error(`no Node.js build for ${platform}-${arch}`);
	const base = `https://nodejs.org/dist/v${NODE_VERSION}/`;
	const sums = await fetchText(`${base}SHASUMS256.txt`);
	const line = sums.split('\n').find((l) => l.trim().endsWith(`  ${name}`));
	if (!line) throw new Error(`no checksum for ${name}`);
	const expected = line.trim().split(/\s+/)[0];
	const archive = path.join(cacheDir, name);
	await download(`${base}${name}`, archive, expected);
	const dir = path.join(outDir, 'node');
	await extract(archive, dir, 1);
	const node = platform === 'win32' ? path.join(dir, 'node.exe') : path.join(dir, 'bin', 'node');
	run(node, ['--version']);
	// Source-managed installations need npm for local updates.
	if (args['runtime-only']) return { version: NODE_VERSION };
	for (const extra of ['node_modules', 'npm', 'npm.cmd', 'npx', 'npx.cmd', 'corepack', 'corepack.cmd', 'include', 'share']) {
		await rm(path.join(dir, extra), { recursive: true, force: true });
	}
	await rm(path.join(dir, 'lib', 'node_modules'), { recursive: true, force: true });
	await rm(path.join(dir, 'bin', 'npm'), { force: true });
	await rm(path.join(dir, 'bin', 'npx'), { force: true });
	await rm(path.join(dir, 'bin', 'corepack'), { force: true });
	return { version: NODE_VERSION };
}

/**
 * The directory inside an extracted archive that holds `marker`: the archive
 * root itself, or a single wrapping directory (the release layout has varied).
 */
export async function findRoot(dir, marker, depth = 2) {
	if (existsSync(path.join(dir, marker))) return dir;
	if (depth === 0) return null;
	const { readdir } = await import('node:fs/promises');
	for (const entry of await readdir(dir, { withFileTypes: true })) {
		if (!entry.isDirectory()) continue;
		const found = await findRoot(path.join(dir, entry.name), marker, depth - 1);
		if (found) return found;
	}
	return null;
}

async function installPostgres() {
	const target = postgresTarget(platform, arch);
	const name = postgresArchiveName(POSTGRES_VERSION, target);
	const url = postgresArchiveUrl(POSTGRES_VERSION, target);
	const expected = parseSha256Sidecar(await fetchText(postgresChecksumUrl(POSTGRES_VERSION, target)), name);
	const archive = path.join(cacheDir, name);
	await download(url, archive, expected);
	const staging = path.join(cacheDir, `postgresql-${POSTGRES_VERSION}-${target}`);
	await extract(archive, staging, 0);
	const marker = initdbRelativePath(platform);
	const root = await findRoot(staging, marker);
	if (!root) throw new Error(`${name} does not contain ${marker}`);
	await rm(postgresDir, { recursive: true, force: true });
	await mkdir(postgresDir, { recursive: true });
	// include/ and the pkg-config files are for building against libpq; the app only runs the server.
	// fs.cp otherwise turns relative Linux/macOS library links into absolute cache paths.
	for (const item of ['bin', 'lib', 'share']) {
		if (existsSync(path.join(root, item))) await cp(path.join(root, item), path.join(postgresDir, item), { recursive: true, verbatimSymlinks: true });
	}
	await rm(staging, { recursive: true, force: true });
	await validateBundledSymlinks(postgresDir, platform);
	if (platform === process.platform) {
		run(path.join(postgresDir, 'bin', platform === 'win32' ? 'pg_ctl.exe' : 'pg_ctl'), ['--version']);
		run(path.join(postgresDir, marker), ['--version']);
	}
	const entry = postgresManifestEntry(POSTGRES_VERSION, target);
	await writeFile(path.join(postgresDir, 'manifest.json'), JSON.stringify({ ...entry, builtAt: new Date().toISOString() }, null, 2));
	return entry;
}

const npmCmd = process.platform === 'win32' ? 'npm.cmd' : 'npm';

async function buildNodePackage(name) {
	const scratch = await mkdtemp(path.join(os.tmpdir(), 'shuffle-package-'));
	const src = path.join(scratch, name);
	const dest = path.join(outDir, 'server', name);
	const env = { ...process.env, PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: '1', PUPPETEER_SKIP_DOWNLOAD: '1' };
	try {
		await cp(path.join(repoDir, name), src, {
			recursive: true,
			verbatimSymlinks: true,
			filter: (file) => !path.relative(path.join(repoDir, name), file).split(path.sep).some((part) => ['node_modules', 'dist', '.git', '.env'].includes(part))
		});
		run(npmCmd, ['ci', '--ignore-scripts', '--no-audit', '--no-fund'], { cwd: src, env, shell: process.platform === 'win32' });
		run(npmCmd, ['run', 'build'], { cwd: src, env, shell: process.platform === 'win32' });
		run(npmCmd, ['prune', '--omit=dev', '--ignore-scripts', '--no-audit', '--no-fund'], { cwd: src, env, shell: process.platform === 'win32' });
		await rm(dest, { recursive: true, force: true });
		await mkdir(dest, { recursive: true });
		for (const item of ['dist', 'node_modules', 'package.json']) {
			await cp(path.join(src, item), path.join(dest, item), { recursive: true, verbatimSymlinks: true });
		}
	} finally {
		await rm(scratch, { recursive: true, force: true });
	}
}

async function copyServer() {
	const dest = path.join(outDir, 'server');
	await mkdir(dest, { recursive: true });
	await cp(path.join(repoDir, 'app'), path.join(dest, 'app'), {
		recursive: true,
		verbatimSymlinks: true,
		filter: (src) => {
			const rel = path.relative(path.join(repoDir, 'app'), src);
			const parts = rel.split(path.sep);
			if (parts.includes('__pycache__') || parts.includes('.pytest_cache')) return false;
			if (parts[0] === 'static') return false;
			return !src.endsWith('.pyc');
		}
	});
	if (!existsSync(path.join(frontendBuild, 'index.html'))) {
		throw new Error('Frontend build missing. Build the frontend separately and pass --frontend-build; runtime assembly never installs into the source checkout.');
	}
	await cp(frontendBuild, path.join(dest, 'app', 'static'), { recursive: true, verbatimSymlinks: true });
	await cp(path.join(repoDir, 'fluid', 'fluid'), path.join(dest, 'fluid'), {
		recursive: true, verbatimSymlinks: true, filter: src => !src.split(path.sep).includes('__pycache__') && !src.endsWith('.pyc')
	});
	for (const file of ['requirements.txt', 'requirements-native.txt', 'LICENSE']) {
		await cp(path.join(repoDir, file), path.join(dest, file));
	}
}

async function installCloudflared() {
	// Use the backend's pins and extraction rules, but never let a client on
	// the build machine's PATH satisfy a dependency of the shipped runtime.
	const server = path.join(outDir, 'server');
	log('bundling checksum-verified cloudflared');
	const result = spawnSync(pythonExe(path.join(outDir, 'python')), ['-B', '-m', 'app.core.cloudflared', '--bundle'], {
		cwd: server,
		env: { ...process.env, PYTHONNOUSERSITE: '1' },
		encoding: 'utf8',
		windowsHide: true
	});
	if (result.error || result.status !== 0) {
		throw new Error(`cloudflared bundle failed: ${result.error?.message ?? result.stderr?.trim() ?? result.status}`);
	}
	const { executable, ...entry } = JSON.parse(result.stdout);
	const relative = path.relative(server, executable).split(path.sep).join('/');
	const expected = `downloads/cloudflared/${entry.version}/cloudflared${platform === 'win32' ? '.exe' : ''}`;
	if (relative !== expected || !existsSync(executable)) {
		throw new Error('cloudflared was not installed in the packaged worker');
	}
	log(`bundled cloudflared ${entry.version}`);
	return { ...entry, path: `server/${relative}` };
}

async function main() {
	const pkg = JSON.parse(await readFile(path.join(desktopDir, 'package.json'), 'utf8'));
	if (!args.stub && (platform !== process.platform || arch !== process.arch)) {
		throw new Error('Build the worker runtime on its target platform and architecture');
	}
	if (args['gh-only'] && !args.stub) {
		const gh = await installGh({ platform, arch, outDir, cacheDir, download, extract, findRoot });
		await validateBundledSymlinks(outDir, platform);
		run(path.join(outDir, gh.binary), ['--version']);
		await writeFile(path.join(outDir, 'manifest.json'), JSON.stringify({ gh, platform, arch, builtAt: new Date().toISOString() }, null, 2));
		log(`assembled GitHub CLI into ${outDir}`);
		return;
	}
	if (args['postgres-only']) {
		await mkdir(cacheDir, { recursive: true });
		await installPostgres();
		log(`assembled PostgreSQL into ${postgresDir}`);
		return;
	}
	// Each step replaces its own directory; --keep-python skips the slow
	// dependency install when iterating locally. CI always starts clean.
	if (!args['keep-python']) await rm(outDir, { recursive: true, force: true });
	await mkdir(outDir, { recursive: true });
	await rm(path.join(outDir, 'manifest.json'), { force: true });
	if (args.stub) {
		await writeFile(path.join(outDir, 'manifest.json'), JSON.stringify({ stub: true, appVersion: pkg.version, builtAt: new Date().toISOString() }, null, 2));
		log(`wrote stub manifest to ${outDir}`);
		return;
	}
	log(`building worker runtime for ${platform}-${arch} into ${outDir}`);
	await mkdir(cacheDir, { recursive: true });
	const python = await installPython();
	const node = await installNode();
	const gh = await installGh({ platform, arch, outDir, cacheDir, download, extract, findRoot });
	run(path.join(outDir, gh.binary), ['--version']);
	let postgres = null;
	if (args['skip-postgres']) log('skipping PostgreSQL (--skip-postgres)');
	else postgres = await installPostgres();
	if (!args['runtime-only']) await copyServer();
	const cloudflared = args['runtime-only'] ? null : await installCloudflared();
	if (!args['runtime-only']) {
		await buildNodePackage('tools');
		await buildNodePackage('browser-mcp');
	}
	// Both resource trees must be relocatable, after all staging directories are gone.
	await validateBundledSymlinks(outDir, platform);
	const manifest = {
		stub: false,
		appVersion: pkg.version,
		runtime: await runtimeContract(repoDir),
		pythonDependencies: await pythonDependencies(repoDir),
		platform,
		arch,
		python,
		node,
		gh,
		postgres,
		cloudflared,
		builtAt: new Date().toISOString(),
		host: os.hostname()
	};
	await writeFile(path.join(outDir, 'manifest.json'), JSON.stringify(manifest, null, 2));
	const size = await dirSize(outDir);
	log(`done: ${(size / 1024 / 1024).toFixed(0)} MB in ${outDir}`);
	if (postgres) log(`PostgreSQL ${postgres.version} (${postgres.target}): ${((await dirSize(postgresDir)) / 1024 / 1024).toFixed(0)} MB in ${postgresDir}`);
}

async function dirSize(dir) {
	const { readdir } = await import('node:fs/promises');
	let total = 0;
	for (const entry of await readdir(dir, { withFileTypes: true })) {
		const full = path.join(dir, entry.name);
		if (entry.isDirectory()) total += await dirSize(full);
		else if (entry.isFile()) total += (await stat(full)).size;
	}
	return total;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	main().catch((error) => {
		console.error(`[build-worker] failed: ${error.message}`);
		process.exit(1);
	});
}
