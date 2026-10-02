#!/usr/bin/env node
/**
 * Build the phone UI (../mobile) into build/mobile, next to the desktop UI.
 *
 * Runs as the last `postbuild` step. Every way a worker gets its UI copies
 * frontend/build into app/static (Docker, the in-app updater, the desktop
 * worker bundle, run_local, deploy scripts), so emitting the phone UI here
 * ships it with every worker, which serves it at /mobile/ (app/mobile_ui.py).
 * The Android and iOS apps load it from the paired worker, which is why a UI
 * change never needs a new app release (docs/mobile-desktop-ui-split.md).
 *
 * A missing mobile/ directory or a failed build fails the frontend build: a
 * worker without /mobile/ leaves the apps with nothing to show. Set
 * SHUFFLE_SKIP_MOBILE_UI=1 only for a desktop-only local iteration.
 */
import { execFileSync } from 'node:child_process';
import { cpSync, existsSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
const mobileRoot = join(frontendRoot, '..', 'mobile');
const output = join(frontendRoot, 'build', 'mobile');

if (process.env.SHUFFLE_SKIP_MOBILE_UI === '1') {
	console.warn('[build-mobile-ui] SHUFFLE_SKIP_MOBILE_UI=1 — skipped; this build has no /mobile/ for the phone apps.');
	process.exit(0);
}
if (!existsSync(join(mobileRoot, 'package.json'))) {
	console.error(`[build-mobile-ui] ${mobileRoot} is missing. The phone apps load their UI from the worker's /mobile/,`);
	console.error('[build-mobile-ui] so the frontend build must include it (set SHUFFLE_SKIP_MOBILE_UI=1 to skip on purpose).');
	process.exit(1);
}

const run = (args) =>
	execFileSync(process.platform === 'win32' ? 'npm.cmd' : 'npm', args, {
		cwd: mobileRoot,
		stdio: 'inherit',
		shell: process.platform === 'win32'
	});

console.log('[build-mobile-ui] building mobile/ (served by the worker at /mobile/)');
// Links mobile/node_modules to this package's node_modules: one Svelte runtime.
run(['run', 'deps']);
run(['run', 'build']);

const built = join(mobileRoot, 'build');
if (!existsSync(join(built, 'index.html'))) {
	console.error('[build-mobile-ui] mobile/build/index.html is missing after the build');
	process.exit(1);
}
rmSync(output, { recursive: true, force: true });
cpSync(built, output, { recursive: true });
console.log('[build-mobile-ui] copied mobile/build -> frontend/build/mobile');
