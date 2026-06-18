import { defineConfig } from 'vitest/config';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { fileURLToPath } from 'node:url';

const lib = fileURLToPath(new URL('./src/lib', import.meta.url));

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
		// SvelteKit `$lib` alias for unit tests (Svelte 5 resolves `svelte`
		// itself via vite-plugin-svelte + the `browser` resolve condition)
		alias: [
			{ find: /^\$lib\/(.*)$/, replacement: `${lib}/$1` },
			{ find: /^\$lib$/, replacement: lib }
		]
	},
	resolve: {
		conditions: ['browser']
	}
});
