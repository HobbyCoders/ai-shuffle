// The mobile shell is selected at build time; nothing about the page or the
// worker can change it (see frontend/src/lib/utils/platform.ts).
process.env.VITE_SHUFFLE_SHELL ??= 'mobile';

import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';
import webConfig from '../frontend/vite.config.js';
import { svelteOptions } from '../frontend/svelte-options.js';

/**
 * The mobile UI: a static, client-only SvelteKit app that every worker serves
 * at /mobile/ and the Android and iOS apps load from the paired worker. The
 * frontend build emits it into frontend/build/mobile
 * (frontend/scripts/build-mobile-ui.mjs), so it ships with the worker and a UI
 * change never needs a new app release. It shares `$lib` with ../frontend; the
 * apps hand it the paired session through `window.__SHUFFLE_MOBILE__`
 * (frontend/src/lib/api/runtime.ts). See docs/mobile-desktop-ui-split.md.
 */

export default defineConfig({
	cacheDir: '.svelte-kit/vite-cache',
	plugins: [
		tailwindcss(),
		sveltekit({
			// Same compiler options and a11y warning filter as the web build —
			// the shared components are compiled here too.
			...svelteOptions,
			adapter: adapter({
				pages: 'build',
				assets: 'build',
				// Single-page app: every path renders the one route client-side.
				fallback: 'index.html',
				precompress: false,
				strict: true
			}),
			// The shared library. `$lib` here is the very same tree the web UI compiles.
			alias: {
				$lib: '../frontend/src/lib'
			},
			paths: {
				// Where workers serve it (app/mobile_ui.py). The Android offline demo
				// serves the same build under this path on its own sandboxed origin.
				base: '/mobile',
				relative: true
			},
			// No timer: services/app-update-watch.ts decides when to ask for a newer build.
			version: {
				pollInterval: 0
			}
		})
	],
	// Build-time constants the shared $lib reads (__APP_VERSION__ in Settings →
	// General). scripts/check-build.mjs fails the build if one is left bare.
	define: webConfig.define,
	// Same chunking budget and vendor splits as the web build — it is the same $lib.
	build: webConfig.build,
	server: {
		// `vite dev` against a local backend: same /api proxy as the web UI, so
		// the mobile shell can be exercised in a desktop browser at
		// http://localhost:5173/mobile/.
		proxy: webConfig.server?.proxy,
		fs: {
			// The shared $lib lives one directory up.
			allow: ['..']
		}
	}
});
