import { readFile } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import { posix } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, mergeConfig, searchForWorkspaceRoot } from 'vite';
import base from '../vite.config.js';
const stubs = fileURLToPath(new URL('./git-panel-stubs.ts', import.meta.url));
// Production modules that reach the network or read the build-time shell flag.
const stubbed = ['/src/lib/stores/git.svelte', '/src/lib/stores/gitOperations.svelte', '/src/lib/stores/github.svelte', '/src/lib/utils/platform'];
// Serve a standalone fixture before SvelteKit's authenticated application shell.
export default mergeConfig(
	base,
	defineConfig({
		server: {
			fs: {
				allow: [
					searchForWorkspaceRoot(process.cwd()),
					realpathSync(new URL('../node_modules', import.meta.url)),
					fileURLToPath(new URL('../../mobile/src', import.meta.url)),
				],
			},
		},
		plugins: [
			{
				name: 'git-panel-fixture',
				enforce: 'pre',
				resolveId(id, importer) {
					const from = importer?.replace(/\\/g, '/').split('?')[0] ?? '';
					// The stubs re-export the real platform helpers.
					if (from.endsWith('/tests/git-panel-stubs.ts')) return null;
					let path = id.replace(/\\/g, '/').replace(/^\$lib/, '/src/lib');
					if (path.startsWith('.') && from) path = posix.join(posix.dirname(from), path);
					path = path.replace(/\.(ts|js)$/, '');
					return stubbed.some((tail) => path.endsWith(tail)) ? stubs : null;
				},
				configureServer(server) {
					server.middlewares.use(async (req, res, next) => {
						if (req.url?.split('?')[0] !== '/tests/git-panel.html')
							return next();
						try {
							const html = await readFile(
								new URL('./git-panel.html', import.meta.url),
								'utf8',
							);
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
