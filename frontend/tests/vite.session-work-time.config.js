import { readFile } from 'node:fs/promises';
import { defineConfig, mergeConfig } from 'vite';
import base from '../vite.config.js';
export default mergeConfig(
	base,
	defineConfig({
		server: { fs: { allow: ['.'] } },
		plugins: [
			{
				name: 'session-work-time-fixture',
				enforce: 'pre',
				configureServer(server) {
					server.middlewares.use(async (req, res, next) => {
						if (req.url?.split('?')[0] !== '/tests/session-work-time.html') return next();
						try {
							const html = await readFile(
								new URL('./session-work-time.html', import.meta.url),
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
