#!/usr/bin/env node
/**
 * Build the phone UI (../mobile) into build/mobile, next to the desktop UI.
 *
 * Every way a worker gets its UI copies frontend/build into app/static
 * (Docker, the in-app updater, the desktop worker bundle, run_local, deploy
 * scripts), so emitting the phone UI here ships it with every worker, which
 * serves it at /mobile/ (app/mobile_ui.py). The Android and iOS apps load it
 * from the paired worker, which is why a UI change never needs a new app
 * release (docs/mobile-desktop-ui-split.md).
 *
 *   node scripts/build-mobile-ui.mjs                  build mobile/, then copy it
 *   node scripts/build-mobile-ui.mjs --with-frontend  `npm run build`: the desktop
 *        UI's `vite build`, then the mobile build
 *   node scripts/build-mobile-ui.mjs --copy           `postbuild`: copy the
 *        finished mobile/build into build/mobile
 *
 * The two builds write disjoint outputs (frontend/build and frontend/.svelte-kit,
 * mobile/build and mobile/.svelte-kit), but mobile reads the frontend's generated
 * tsconfig for its shared sources and Vite config. SvelteKit rewrites that file
 * during desktop config resolution AND in postbuild analysis/prerender workers,
 * even after a CLI sync. Finish desktop before starting mobile so none of those
 * writes can race mobile's reads. The copy waits for both, because the desktop
 * UI's adapter replaces frontend/build when its build finishes.
 *
 * A missing mobile/ directory or a failed build fails the frontend build: a
 * worker without /mobile/ leaves the apps with nothing to show. Set
 * SHUFFLE_SKIP_MOBILE_UI=1 only for a desktop-only local iteration.
 */
import { spawn } from 'node:child_process';
import { cpSync, existsSync, rmSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
const mobileRoot = join(frontendRoot, '..', 'mobile');
const output = join(frontendRoot, 'build', 'mobile');
const built = join(mobileRoot, 'build');

const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';

/**
 * @param {string} command
 * @param {string[]} args
 * @param {string} cwd
 * @returns {Promise<void>}
 */
function run(command, args, cwd) {
	return new Promise((done, fail) => {
		const child = spawn(command, args, { cwd, stdio: 'inherit', shell: process.platform === 'win32' && command === npm });
		child.once('error', fail);
		child.once('exit', (code, signal) => {
			if (code === 0) done();
			else fail(new Error(`${[command, ...args].join(' ')} (in ${cwd}) failed (${signal ?? code})`));
		});
	});
}

/**
 * Keep every desktop sync (including postbuild workers) ahead of mobile's reads.
 * @param {() => Promise<void>} frontend
 * @param {() => Promise<void>} mobile
 */
export async function buildUIs(frontend, mobile) {
	await frontend();
	await mobile();
}

function requireMobile() {
	if (!existsSync(join(mobileRoot, 'package.json'))) {
		console.error(`[build-mobile-ui] ${mobileRoot} is missing. The phone apps load their UI from the worker's /mobile/,`);
		console.error('[build-mobile-ui] so the frontend build must include it (set SHUFFLE_SKIP_MOBILE_UI=1 to skip on purpose).');
		process.exit(1);
	}
}

function copy() {
	if (!existsSync(join(built, 'index.html'))) {
		console.error('[build-mobile-ui] mobile/build/index.html is missing after the build');
		process.exit(1);
	}
	rmSync(output, { recursive: true, force: true });
	cpSync(built, output, { recursive: true });
	console.log('[build-mobile-ui] copied mobile/build -> frontend/build/mobile');
}

/** @param {string} mode */
async function main(mode) {
	const skip = process.env.SHUFFLE_SKIP_MOBILE_UI === '1';
	const frontend = () => run(process.execPath, [join(frontendRoot, 'node_modules', 'vite', 'bin', 'vite.js'), 'build'], frontendRoot);
	if (skip) {
		console.warn('[build-mobile-ui] SHUFFLE_SKIP_MOBILE_UI=1 — skipped; this build has no /mobile/ for the phone apps.');
		if (mode === 'with-frontend') await frontend();
		return;
	}
	requireMobile();
	if (mode === 'copy') return copy();
	// Links mobile/node_modules to this package's node_modules: one Svelte runtime.
	await run(npm, ['run', 'deps'], mobileRoot);
	console.log('[build-mobile-ui] building mobile/ (served by the worker at /mobile/)');
	const mobile = () => run(npm, ['run', 'build'], mobileRoot);
	if (mode !== 'with-frontend') {
		await mobile();
		return copy();
	}
	console.log('[build-mobile-ui] building the desktop UI, then the phone UI (shared generated tsconfig)');
	await buildUIs(frontend, mobile);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	const args = process.argv.slice(2);
	const mode = args.includes('--with-frontend') ? 'with-frontend' : args.includes('--copy') ? 'copy' : 'mobile';
	main(mode).catch((error) => {
		console.error(`[build-mobile-ui] ${error.message}`);
		process.exit(1);
	});
}
