#!/usr/bin/env node
// Run only the Vitest files a change can reach
// (`node scripts/vitest-changed.mjs <base>`) in frontend/ and mobile/.
// A full run (`scripts/local_ci.py full`) still runs every test.
//
// Vitest's own `--changed` follows imports only. Many tests here read a
// component's source as text (readFileSync of MessageArea.svelte, app.html,
// a Python file), and a change to that file would skip them. So the related
// set is the changed files plus every test file that names a changed file.
// A dependency or config change (forceRerunTriggers in vitest.config.ts)
// still runs everything.
import { execFileSync, spawnSync } from 'node:child_process';
import { readdirSync, readFileSync } from 'node:fs';
import { basename, join, relative, resolve } from 'node:path';

const TEST_FILE = /\.(test|spec)\.(js|ts)$/;

// Vitest module ids use forward slashes on every platform.
/** @param {string} path */
const slashes = (path) => path.split('\\').join('/');

/**
 * @param {string} base
 * @returns {string[]}
 */
export function changedFiles(base, cwd = process.cwd()) {
	const root = execFileSync('git', ['rev-parse', '--show-toplevel'], { cwd, encoding: 'utf8' }).trim();
	const names = execFileSync('git', ['diff', '--name-only', `${base}...HEAD`], { cwd, encoding: 'utf8' });
	return names
		.split('\n')
		.map((line) => line.trim())
		.filter(Boolean)
		.map((name) => slashes(resolve(root, name)));
}

/**
 * @param {string} dir
 * @returns {string[]}
 */
export function testFiles(dir) {
	/** @type {string[]} */
	const found = [];
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		const path = join(dir, entry.name);
		if (entry.isDirectory()) found.push(...testFiles(path));
		else if (TEST_FILE.test(entry.name)) found.push(slashes(path));
	}
	return found;
}

// Test files whose text names a changed file (by file name, the way the
// source-reading tests spell their paths).
/**
 * @param {string[]} changed
 * @param {string[]} tests
 * @param {(path: string) => string} [read]
 * @returns {string[]}
 */
export function textReferences(changed, tests, read = (path) => readFileSync(path, 'utf8')) {
	const names = [...new Set(changed.map((path) => basename(path)))];
	if (names.length === 0) return [];
	return tests.filter((test) => {
		const text = read(test);
		return names.some((name) => text.includes(name));
	});
}

function main() {
	const base = process.argv[2];
	if (!base) {
		console.error('usage: vitest-changed.mjs <base-ref>');
		process.exit(2);
	}
	const changed = changedFiles(base);
	const referenced = textReferences(changed, testFiles(resolve('src')));
	const related = [...new Set([...changed, ...referenced])];
	console.log(
		`${changed.length} changed file(s); ${referenced.length} test file(s) name one of them.`
	);
	for (const path of referenced) console.log(`  + ${relative(process.cwd(), path)}`);
	if (related.length === 0) {
		console.log('Nothing changed: no tests to run.');
		return;
	}
	const result = spawnSync(
		'npx',
		['vitest', 'related', '--run', '--passWithNoTests', ...related],
		{ stdio: 'inherit', shell: process.platform === 'win32' }
	);
	process.exit(result.status ?? 1);
}

if (slashes(process.argv[1] ?? '').endsWith('scripts/vitest-changed.mjs')) {
	main();
}
