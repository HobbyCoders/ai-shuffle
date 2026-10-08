import { defineConfig } from 'vitest/config';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { fileURLToPath } from 'node:url';
import { appVersion } from './app-version.js';

const lib = fileURLToPath(new URL('./src/lib', import.meta.url));
const stubs = fileURLToPath(new URL('./src/lib/test-stubs', import.meta.url));

// Vitest's defaults (package.json, vite/vitest config) plus the lockfile,
// compiler config and the SvelteKit stubs every store test runs on.
// ../mobile/vitest.config.ts keeps the same list.
const FULL_RUN_TRIGGERS = [
	'**/package.json/**',
	'**/{vitest,vite}.config.*/**',
	'**/package-lock.json',
	'**/svelte.config.js',
	'**/tsconfig.json',
	'**/app-version.js',
	'**/src/lib/test-stubs/**',
	'**/scripts/vitest-changed.mjs',
	'**/.github/workflows/ci-frontend.yml'
];

export default defineConfig({
	// Keep generated caches local when dependencies are shared by worktrees.
	cacheDir: '.svelte-kit/vitest-cache',
	plugins: [svelte({ hot: !process.env.VITEST })],
	define: {
		__APP_VERSION__: JSON.stringify(appVersion)
	},
	server: {
		fs: {
			// The docs registry imports Markdown from the sibling content directory.
			allow: [
				fileURLToPath(new URL('./', import.meta.url)),
				fileURLToPath(new URL('../docs/content/', import.meta.url))
			]
		}
	},
	test: {
		globals: true,
		environment: 'jsdom',
		include: ['src/**/*.{test,spec}.{js,ts}'],
		exclude: ['node_modules', 'build', '.svelte-kit'],
		// A change to any of these runs every test, including in CI's
		// changed-only PR runs (scripts/vitest-changed.mjs).
		forceRerunTriggers: FULL_RUN_TRIGGERS,
		coverage: {
			provider: 'v8',
			reporter: ['text', 'json', 'html'],
			exclude: [
				'node_modules/',
				'src/**/*.d.ts',
				'src/**/*.test.ts',
				'src/**/*.spec.ts',
				'.svelte-kit/',
				'build/'
			]
		},
		// SvelteKit `$lib` alias for unit tests. (Svelte 5 resolves `svelte`
		// itself via vite-plugin-svelte + the `browser` resolve condition — do
		// NOT alias `svelte` → `svelte/internal`: that was a Svelte 4 hack and
		// `svelte/internal` no longer exists in Svelte 5.)
		alias: [
			{ find: /^\$lib\/(.*)$/, replacement: `${lib}/$1` },
			{ find: /^\$lib$/, replacement: lib },
			// SvelteKit virtual modules aren't available outside the kit plugin;
			// stub them so store code that imports them can be unit-tested.
			{ find: /^\$app\/environment$/, replacement: `${stubs}/app-environment.ts` },
			{ find: /^\$app\/stores$/, replacement: `${stubs}/app-stores.ts` },
			{ find: /^\$app\/navigation$/, replacement: `${stubs}/app-navigation.ts` },
			{ find: /^\$app\/state$/, replacement: `${stubs}/app-state.svelte.ts` }
		]
	},
	resolve: {
		conditions: ['browser']
	}
});
