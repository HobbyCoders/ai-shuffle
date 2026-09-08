import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/**
 * The mobile UI: a static, client-only SvelteKit app that the Android app
 * loads from its own assets at
 * https://appassets.androidplatform.net/assets/mobile-ui/ (WebViewAssetLoader).
 * It shares `$lib` with ../frontend and talks to a worker only through the
 * worker's HTTP/WebSocket APIs (see frontend/src/lib/api/runtime.ts).
 */

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),

	// Same a11y warning filter as ../frontend/svelte.config.js — the shared
	// components are compiled here too.
	onwarn: (warning, handler) => {
		if (warning.code === 'a11y_label_has_associated_control') return;
		if (warning.code === 'a11y_consider_explicit_label') return;
		if (warning.code === 'a11y_click_events_have_key_events') return;
		if (warning.code === 'a11y_no_static_element_interactions') return;
		if (warning.code === 'a11y_interactive_supports_focus') return;
		handler(warning);
	},

	kit: {
		adapter: adapter({
			pages: 'build',
			assets: 'build',
			// Single-page app: every path renders the one route client-side.
			fallback: 'index.html',
			precompress: false,
			strict: true
		}),
		// The shared library. `$lib` here is the very same tree the web UI compiles.
		files: {
			lib: '../frontend/src/lib'
		},
		paths: {
			// Where the Android app mounts the bundle (WebViewAssetLoader path).
			base: '/assets/mobile-ui',
			relative: true
		}
	}
};

export default config;
