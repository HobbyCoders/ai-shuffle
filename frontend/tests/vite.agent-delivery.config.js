import { readFile } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import { defineConfig, mergeConfig, searchForWorkspaceRoot } from 'vite';
import base from '../vite.config.js';
// Serve a standalone fixture before SvelteKit's authenticated application shell.
export default mergeConfig(
	base,
	defineConfig({
		server: {
			fs: {
				allow: [searchForWorkspaceRoot(process.cwd()), realpathSync(new URL('../node_modules', import.meta.url))],
			},
		},
		plugins: [
			{
				name: 'agent-delivery-fixture',
				enforce: 'pre',
				configureServer(server) {
					server.middlewares.use(async (req, res, next) => {
						if (req.url?.split('?')[0] !== '/tests/agent-delivery.html')
							return next();
						try {
							const html = await readFile(
								new URL('./agent-delivery.html', import.meta.url),
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
