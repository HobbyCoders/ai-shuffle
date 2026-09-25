// The mobile shell is selected at build time; nothing about the page or the
// worker can change it (see frontend/src/lib/utils/platform.ts).
process.env.VITE_SHUFFLE_SHELL ??= 'mobile';

import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';
import webConfig from '../frontend/vite.config.js';

export default defineConfig({
	cacheDir: '.svelte-kit/vite-cache',
	plugins: [tailwindcss(), sveltekit()],
	// Build-time constants the shared $lib reads (__APP_VERSION__ in Settings →
	// General). scripts/check-build.mjs fails the build if one is left bare.
	define: webConfig.define,
	// Same chunking budget and vendor splits as the web build — it is the same $lib.
	build: webConfig.build,
	server: {
		// `vite dev` against a local backend: same /api proxy as the web UI, so
		// the mobile shell can be exercised in a desktop browser at
		// http://localhost:5173/assets/mobile-ui/ before it is bundled.
		proxy: webConfig.server?.proxy,
		fs: {
			// The shared $lib lives one directory up.
			allow: ['..']
		}
	}
});
