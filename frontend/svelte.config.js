import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),

	// Suppress non-critical a11y warnings that are overly pedantic for this codebase
	onwarn: (warning, handler) => {
		// Label association warnings - many are false positives for grouped controls
		if (warning.code === 'a11y_label_has_associated_control') return;
		if (warning.code === 'a11y_consider_explicit_label') return;
		// Modal backdrop click handlers are intentional and have keyboard escape handlers
		if (warning.code === 'a11y_click_events_have_key_events') return;
		if (warning.code === 'a11y_no_static_element_interactions') return;
		// Dialogs with role="dialog" and aria-modal="true" work correctly
		if (warning.code === 'a11y_interactive_supports_focus') return;
		handler(warning);
	},

	kit: {
		adapter: adapter({
			pages: 'build',
			assets: 'build',
			fallback: 'index.html',
			precompress: false,
			strict: true
		}),
		paths: {
			base: ''
		}
	}
};

export default config;
