import { defineConfig } from 'vitest/config';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { fileURLToPath } from 'node:url';
import { appVersion } from '../frontend/app-version.js';

// Tests here exercise the mobile shell: same build flag as `vite build`.
process.env.VITE_SHUFFLE_SHELL = 'mobile';

const lib = fileURLToPath(new URL('../frontend/src/lib', import.meta.url));
const stubs = fileURLToPath(new URL('../frontend/src/lib/test-stubs', import.meta.url));

export default defineConfig({
	cacheDir: '.svelte-kit/vitest-cache',
	plugins: [svelte({ hot: !process.env.VITEST })],
	define: {
		__APP_VERSION__: JSON.stringify(appVersion)
	},
	test: {
		globals: true,
		environment: 'jsdom',
		include: ['src/**/*.{test,spec}.{js,ts}'],
		exclude: ['node_modules', 'build', '.svelte-kit'],
		// Mirrors ../frontend/vitest.config.ts: `$lib` is the shared library and
		// the SvelteKit virtual modules are stubbed.
		alias: [
			{ find: /^\$lib\/(.*)$/, replacement: `${lib}/$1` },
			{ find: /^\$lib$/, replacement: lib },
			{ find: /^\$app\/environment$/, replacement: `${stubs}/app-environment.ts` },
			{ find: /^\$app\/stores$/, replacement: `${stubs}/app-stores.ts` },
			{ find: /^\$app\/navigation$/, replacement: `${stubs}/app-navigation.ts` }
		]
	},
	resolve: {
		conditions: ['browser']
	}
});
