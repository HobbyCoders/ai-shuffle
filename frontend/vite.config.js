import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit()],
	build: {
		// Post-split budget. The primary route no longer carries the feature
		// panels (Settings/Notes/Build/Docs/Stats/Automations/Files/Browser/
		// Diff), the docs markdown or the Shiki grammars — each of those is a
		// dynamically-imported chunk now (see deck/lazy/panels.ts, docs/index.ts
		// and utils/shiki.ts). What's left on the critical path is the chat
		// surface plus the rich-text editor, so an 800 kB advisory limit is a
		// budget again rather than a rubber stamp: if a chunk crosses it,
		// something eagerly-imported grew and it's worth a look.
		chunkSizeWarningLimit: 800,
		rollupOptions: {
			output: {
				// Split heavy vendor libs into their own chunks. This
				// improves browser caching (deploys that don't touch these
				// deps keep the same chunk hash) and keeps the main bundle
				// smaller. Rollup may still merge chunks when dependency
				// graphs overlap, but the hints materially help here.
				manualChunks(id) {
					// Docs markdown: ~404 kB of raw strings, wanted only by
					// readers who actually open Docs. One chunk instead of ~35
					// so opening Docs is a single request.
					if (id.includes('/src/lib/docs/content/')) return 'docs-content';

					if (id.includes('node_modules')) {
						if (id.includes('@tiptap') || id.includes('prosemirror')) return 'vendor-tiptap';
						if (id.includes('marked')) return 'vendor-marked';
						if (id.includes('dompurify')) return 'vendor-dompurify';
						if (id.includes('lucide-svelte')) return 'vendor-lucide';
						// Shiki's runtime (core + textmate + JS regex engine +
						// themes) is one cacheable chunk, loaded on first code
						// block. NOTE: @shikijs/langs is deliberately excluded —
						// each grammar must stay its own chunk so a transcript
						// only pays for the languages it actually contains.
						if (id.includes('@shikijs/langs') || id.includes('shiki/dist/langs')) return;
						if (
							id.includes('/shiki/') ||
							id.includes('@shikijs/core') ||
							id.includes('@shikijs/engine-javascript') ||
							id.includes('@shikijs/themes') ||
							id.includes('@shikijs/types') ||
							id.includes('@shikijs/vscode-textmate')
						) {
							return 'vendor-shiki';
						}
					}
				}
			}
		}
	},
	server: {
		proxy: {
			'/api': {
				target: 'http://localhost:8000',
				changeOrigin: true,
				secure: false,
				cookieDomainRewrite: {
					'*': ''
				},
				cookiePathRewrite: {
					'*': '/'
				}
			},
			'/health': {
				target: 'http://localhost:8000',
				changeOrigin: true
			}
		}
	}
});
