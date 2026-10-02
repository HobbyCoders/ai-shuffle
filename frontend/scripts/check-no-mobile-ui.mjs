#!/usr/bin/env node
/**
 * Guard: the desktop web UI must not carry the mobile shell.
 *
 * The mobile UI lives in mobile/ (its own SvelteKit app, which the worker
 * serves to the phone apps at /mobile/). Nothing under frontend/src — and
 * nothing in the desktop build output — may select on the mobile platform
 * attribute: the web UI is desktop-only by decision, at every window width
 * (docs/mobile-desktop-ui-split.md).
 *
 * Runs as part of `npm run check` and after `npm run build`. Exits 1 with the
 * offending locations when the literal `data-platform='mobile'` /
 * `data-platform="mobile"` appears in frontend/src or frontend/build. The
 * phone UI's own output, build/mobile (scripts/build-mobile-ui.mjs), is the
 * one place those rules belong and is skipped.
 */
import { readdirSync, readFileSync, statSync, existsSync } from 'node:fs';
import { join, relative, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
const roots = ['src', 'build'].map((d) => join(frontendRoot, d)).filter(existsSync);
const NEEDLE = /data-platform=(['"])mobile\1/;
const TEXT = /\.(svelte|ts|js|mjs|css|html|json|txt|md)$/;

const phoneBuild = join(frontendRoot, 'build', 'mobile');

function* walk(dir) {
	for (const name of readdirSync(dir)) {
		const path = join(dir, name);
		if (path === phoneBuild) continue;
		if (statSync(path).isDirectory()) yield* walk(path);
		else if (TEXT.test(name)) yield path;
	}
}

const hits = [];
for (const root of roots) {
	for (const file of walk(root)) {
		const text = readFileSync(file, 'utf8');
		if (!NEEDLE.test(text)) continue;
		text.split('\n').forEach((line, i) => {
			if (NEEDLE.test(line)) hits.push(`${relative(frontendRoot, file)}:${i + 1}: ${line.trim().slice(0, 120)}`);
		});
	}
}

if (hits.length) {
	console.error('[check-no-mobile-ui] The web UI must not contain mobile-shell rules. Move them to mobile/src/mobile.css:');
	for (const hit of hits) console.error('  ' + hit);
	process.exit(1);
}
console.log(`[check-no-mobile-ui] ok — no mobile-shell selectors under ${roots.map((r) => relative(frontendRoot, r)).join(', ')}`);
