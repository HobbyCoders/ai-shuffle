import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';
import { appVersion } from './app-version.js';

export default defineConfig({
	// Worktrees may share installed packages; keep generated caches in this tree.
	cacheDir: '.svelte-kit/vite-cache',
	plugins: [tailwindcss(), sveltekit()],
	define: {
		__APP_VERSION__: JSON.stringify(appVersion)
	},
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
				// Set AI_SHUFFLE_DEV_API=http://host:8000 to review the UI against a
				// remote backend. The backend's CSRF check compares Origin with the
				// forwarded host, so we forward the browser-facing Host explicitly.
				target: process.env.AI_SHUFFLE_DEV_API || 'http://localhost:8000',
				configure(proxy) {
					proxy.on('proxyReq', (proxyReq, req) => {
						if (req.headers.host) proxyReq.setHeader('x-forwarded-host', req.headers.host);
					});
				},
				// Chat and transcription use same-origin WebSockets, including LAN previews.
				ws: true,
				// Preserve the browser-facing Host so the backend can validate Origin
				// against it. Rewriting Host to localhost breaks CSRF on LAN logins.
				changeOrigin: false,
				secure: false,
				cookieDomainRewrite: {
					'*': ''
				},
				cookiePathRewrite: {
					'*': '/'
				}
			},
			'/health': {
				target: process.env.AI_SHUFFLE_DEV_API || 'http://localhost:8000',
				changeOrigin: true
			}
		}
	}
});
