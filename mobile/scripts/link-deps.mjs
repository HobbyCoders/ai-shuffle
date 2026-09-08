#!/usr/bin/env node
/**
 * Link mobile/node_modules to ../frontend/node_modules.
 *
 * The mobile app shares every dependency (and every version) with the web
 * frontend because it compiles the same `$lib` sources. One node_modules tree
 * keeps a single copy of svelte/vite on disk — two copies would give the
 * shared components two Svelte runtimes. Run `npm ci` in frontend/ first.
 */
import { existsSync, lstatSync, rmSync, symlinkSync, readlinkSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const mobileRoot = resolve(here, '..');
const target = resolve(mobileRoot, '..', 'frontend', 'node_modules');
const link = resolve(mobileRoot, 'node_modules');

if (!existsSync(target)) {
	console.error(`[mobile] ${target} is missing — run \`npm ci\` in frontend/ first.`);
	process.exit(1);
}
if (existsSync(link) || isDangling(link)) {
	const stat = lstatSync(link);
	if (stat.isSymbolicLink() && resolve(mobileRoot, readlinkSync(link)) === target) {
		console.log('[mobile] node_modules already links to frontend/node_modules');
		process.exit(0);
	}
	if (!stat.isSymbolicLink()) {
		console.error(`[mobile] ${link} exists and is a real directory; remove it to share frontend/node_modules.`);
		process.exit(1);
	}
	rmSync(link, { recursive: false, force: true });
}
symlinkSync(target, link, process.platform === 'win32' ? 'junction' : 'dir');
console.log(`[mobile] linked node_modules -> ${target}`);

function isDangling(path) {
	try { lstatSync(path); return true; } catch { return false; }
}
