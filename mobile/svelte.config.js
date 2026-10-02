import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/**
 * The mobile UI: a static, client-only SvelteKit app that every worker serves
 * at /mobile/ and the Android and iOS apps load from the paired worker. The
 * frontend build emits it into frontend/build/mobile
 * (frontend/scripts/build-mobile-ui.mjs), so it ships with the worker and a UI
 * change never needs a new app release. It shares `$lib` with ../frontend; the
 * apps hand it the paired session through `window.__SHUFFLE_MOBILE__`
 * (frontend/src/lib/api/runtime.ts). See docs/mobile-desktop-ui-split.md.
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
			// Where workers serve it (app/mobile_ui.py). The Android offline demo
			// serves the same build under this path on its own sandboxed origin.
			base: '/mobile',
			relative: true
		}
	}
};

export default config;
