/** Pinned GitHub CLI assets; digests copied from the upstream release's
 * https://github.com/cli/cli/releases/download/v2.102.0/gh_2.102.0_checksums.txt
 * Never fetch checksums at build time: only these reviewed pins are trusted.
 */
import { chmod, cp, mkdir, mkdtemp, rm, stat } from 'node:fs/promises';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const native = JSON.parse(readFileSync(new URL('../runtime-contract.json', import.meta.url), 'utf8')).native;
export const GH_VERSION = native.gh.version;
const RELEASE = `https://github.com/cli/cli/releases/download/v${GH_VERSION}`;
export const GH_ASSETS = native.gh.assets;

export function ghAsset(platform, arch) {
	const pin = GH_ASSETS[`${platform}-${arch}`];
	if (!pin) throw new Error(`no GitHub CLI binaries for ${platform}-${arch}`);
	const name = `gh_${GH_VERSION}_${pin[0]}`;
	return { name, url: `${RELEASE}/${name}`, sha256: pin[1] };
}

/** Share build-worker's verified download/cache and archive extraction helpers. */
export async function installGh({ platform, arch, outDir, cacheDir, download, extract, findRoot }) {
	const asset = ghAsset(platform, arch);
	await mkdir(cacheDir, { recursive: true });
	const archive = path.join(cacheDir, asset.name);
	await download(asset.url, archive, asset.sha256);
	const staging = await mkdtemp(path.join(cacheDir, 'gh-unpack-'));
	const binary = platform === 'win32' ? 'gh.exe' : 'gh';
	try {
		await extract(archive, staging, 0);
		const root = await findRoot(staging, `bin/${binary}`);
		if (!root) throw new Error(`${asset.name} does not contain bin/${binary}`);
		// Install only the executable and its license, not docs/completions.
		for (const item of [`bin/${binary}`, 'LICENSE']) {
			if (!(await stat(path.join(root, item))).isFile()) throw new Error(`not a file: ${item}`);
		}
		await mkdir(path.join(outDir, 'bin'), { recursive: true });
		await cp(path.join(root, 'bin', binary), path.join(outDir, 'bin', binary));
		if (platform !== 'win32') await chmod(path.join(outDir, 'bin', binary), 0o755);
		await cp(path.join(root, 'LICENSE'), path.join(outDir, 'bin', 'gh-LICENSE'));
	} finally {
		await rm(staging, { recursive: true, force: true });
	}
	return { version: GH_VERSION, source: 'cli/cli', platform, arch,
		archive: asset.name, sha256: asset.sha256, binary: `bin/${binary}`, license: 'bin/gh-LICENSE' };
}
