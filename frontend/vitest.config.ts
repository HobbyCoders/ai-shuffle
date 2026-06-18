import { defineConfig } from 'vitest/config';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { fileURLToPath } from 'node:url';

const lib = fileURLToPath(new URL('./src/lib', import.meta.url));
const stubs = fileURLToPath(new URL('./src/lib/test-stubs', import.meta.url));

export default defineConfig({
	plugins: [svelte({ hot: !process.env.VITEST })],
	test: {
		globals: true,
		environment: 'jsdom',
		include: ['src/**/*.{test,spec}.{js,ts}'],
		exclude: ['node_modules', 'build', '.svelte-kit'],
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
		// SvelteKit `$lib` alias for unit tests. (Do NOT alias `svelte` →
		// `svelte/internal`: that was a Svelte 4 hack and `svelte/internal` no
		// longer exists in Svelte 5, so it breaks any module importing runes
		// helpers like `untrack` from 'svelte'.)
		alias: [
			{ find: /^\$lib\/(.*)$/, replacement: `${lib}/$1` },
			{ find: /^\$lib$/, replacement: lib },
			// SvelteKit virtual modules aren't available outside the kit plugin;
			// stub them so store code that imports them can be unit-tested.
			{ find: /^\$app\/environment$/, replacement: `${stubs}/app-environment.ts` },
			{ find: /^\$app\/navigation$/, replacement: `${stubs}/app-navigation.ts` }
		]
	},
	resolve: {
		conditions: ['browser']
	}
});
