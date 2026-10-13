import { readFile } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import { posix } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, mergeConfig, searchForWorkspaceRoot } from 'vite';
import base from '../vite.config.js';
const stubs = fileURLToPath(new URL('./assistant-setup-stubs.ts', import.meta.url));
// App-wide stores and services AssistantHome uses besides its own API.
const stubbed = [
	'/src/lib/stores/app-data.svelte',
	'/src/lib/stores/layout.svelte',
	'/src/lib/api/voice',
	'/src/lib/services/voiceCall',
	'/src/lib/services/session-navigation',
];
// Serve a standalone fixture before SvelteKit's authenticated application shell.
export default mergeConfig(
	base,
	defineConfig({
		server: {
			fs: {
				allow: [
					searchForWorkspaceRoot(process.cwd()),
					realpathSync(new URL('../node_modules', import.meta.url)),
				],
			},
		},
		plugins: [
			{
				name: 'assistant-setup-fixture',
				enforce: 'pre',
				resolveId(id, importer) {
					const from = importer?.replace(/\\/g, '/').split('?')[0] ?? '';
					let path = id.replace(/\\/g, '/').replace(/^\$lib/, '/src/lib');
					if (path.startsWith('.') && from) path = posix.join(posix.dirname(from), path);
					path = path.replace(/\.(ts|js)$/, '');
					return stubbed.some((tail) => path.endsWith(tail)) ? stubs : null;
				},
				configureServer(server) {
					server.middlewares.use(async (req, res, next) => {
						if (req.url?.split('?')[0] !== '/tests/assistant-setup.html') return next();
						try {
							const html = await readFile(new URL('./assistant-setup.html', import.meta.url), 'utf8');
							res.setHeader('Content-Type', 'text/html');
							res.end(await server.transformIndexHtml(req.url, html));
						} catch (error) {
							next(error);
						}
					});
				},
			},
		],
	}),
);
