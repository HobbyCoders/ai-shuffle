import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit()],
	build: {
		// The main page bundle (~1.3 MB min / ~365 kB gzipped with the
		// vendor splits below) is driven by the rich-text editor
		// (TipTap/ProseMirror), markdown renderer, and the breadth of
		// session-management UI — all on the primary route. Lazy-loading
		// these would add loading spinners to core interactions. Raising
		// the advisory limit reflects that reality; guard the gzipped
		// size in CI if we want a harder budget.
		chunkSizeWarningLimit: 1500,
		rollupOptions: {
			output: {
				// Split heavy vendor libs into their own chunks. This
				// improves browser caching (deploys that don't touch these
				// deps keep the same chunk hash) and keeps the main bundle
				// smaller. Rollup may still merge chunks when dependency
				// graphs overlap, but the hints materially help here.
				manualChunks(id) {
					if (id.includes('node_modules')) {
						if (id.includes('@tiptap') || id.includes('prosemirror')) return 'vendor-tiptap';
						if (id.includes('marked')) return 'vendor-marked';
						if (id.includes('dompurify')) return 'vendor-dompurify';
						if (id.includes('lucide-svelte')) return 'vendor-lucide';
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
