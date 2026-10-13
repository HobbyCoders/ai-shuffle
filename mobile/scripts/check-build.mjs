#!/usr/bin/env node
/**
 * Post-build guard for the phone UI bundle every worker serves at /mobile/
 * (the Android and iOS apps load it from the paired worker).
 *
 * The shared `$lib` reads build-time constants (`__APP_VERSION__`) that Vite
 * only replaces when this package's vite.config.js passes the web build's
 * `define` through. A bare identifier left in the output is a ReferenceError
 * at render time on the phone (Settings → General), so fail the build instead.
 */
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const build = join(root, 'build');
const CONSTANTS = ['__APP_VERSION__'];

function* files(dir) {
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		const path = join(dir, entry.name);
		if (entry.isDirectory()) yield* files(path);
		else if (/\.(m?js|html)$/.test(entry.name)) yield path;
	}
}

let scanned = 0;
const leaks = [];
for (const file of files(build)) {
	scanned++;
	const text = readFileSync(file, 'utf8');
	for (const name of CONSTANTS) {
		if (text.includes(name)) leaks.push(`${relative(root, file)}: ${name}`);
	}
}

if (scanned === 0) {
	console.error('[mobile] check-build: no JS/HTML found under build/');
	process.exit(1);
}
if (leaks.length) {
	console.error('[mobile] check-build: build-time constants were not replaced (vite.config.js `define`):');
	for (const leak of leaks) console.error(`  ${leak}`);
	process.exit(1);
}
console.log(`[mobile] check-build: ${scanned} files, no unreplaced build-time constants`);
