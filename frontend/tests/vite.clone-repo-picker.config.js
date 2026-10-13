import { readFile } from 'node:fs/promises';
import { defineConfig, mergeConfig } from 'vite';
import base from '../vite.config.js';
// Serve a standalone fixture before SvelteKit's authenticated application shell.
export default mergeConfig(
	base,
	defineConfig({
		server: { fs: { allow: ['.'] } },
		plugins: [
			{
				name: 'clone-repo-picker-fixture',
				enforce: 'pre',
				configureServer(server) {
					server.middlewares.use(async (req, res, next) => {
						if (req.url?.split('?')[0] !== '/tests/clone-repo-picker.html') return next();
						try {
							const html = await readFile(
								new URL('./clone-repo-picker.html', import.meta.url),
								'utf8'
							);
							res.setHeader('Content-Type', 'text/html');
							res.end(await server.transformIndexHtml(req.url, html));
						} catch (error) {
							next(error);
						}
					});
				}
			}
		]
	})
);
