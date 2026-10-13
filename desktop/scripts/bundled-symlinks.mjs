import { lstat, readdir, readlink, realpath } from 'node:fs/promises';
import path from 'node:path';

/** Reject non-relocatable links, including relative links that escape through another link. */
export async function validateBundledSymlinks(dir, platform = process.platform, strict = false) {
	if (strict) {
		const stat = await lstat(dir);
		if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error(`Bundled root is a link or not a directory: ${dir}`);
	}
	if (platform === 'win32' && !strict) return;
	const root = await realpath(dir);
	const isOutside = (target) => {
		const relative = path.relative(root, target);
		return relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative);
	};
	async function walk(current) {
		for (const entry of await readdir(current, { withFileTypes: true })) {
			const file = path.join(current, entry.name);
			if (entry.isSymbolicLink()) {
				const target = await readlink(file);
				if (path.isAbsolute(target)) throw new Error(`Bundled symlink is absolute: ${file} -> ${target}`);
				if (isOutside(path.resolve(current, target)) || isOutside(await realpath(file))) {
					throw new Error(`Bundled symlink resolves outside resource directory: ${file} -> ${target}`);
				}
			} else if (entry.isDirectory()) {
				await walk(file);
			}
		}
	}
	await walk(root);
}
