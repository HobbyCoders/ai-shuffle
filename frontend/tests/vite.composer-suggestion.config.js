import { readFile } from 'node:fs/promises';
import { defineConfig, mergeConfig } from 'vite';
import base from '../vite.config.js';
export default mergeConfig(
	base,
	defineConfig({
		server: { fs: { allow: ['.'] } },
		plugins: [
			{
				name: 'composer-suggestion-fixture',
				enforce: 'pre',
				configureServer(server) {
					server.middlewares.use(async (req, res, next) => {
						if (req.url?.split('?')[0] !== '/tests/composer-suggestion.html') return next();
						try {
							const html = await readFile(
								new URL('./composer-suggestion.html', import.meta.url),
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
