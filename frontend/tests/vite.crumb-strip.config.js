import { readFile } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import { defineConfig, mergeConfig, searchForWorkspaceRoot } from 'vite';
import base from '../vite.config.js';
// Serve the standalone desktop crumb-strip fixture before SvelteKit's authenticated
// shell. No module stubs: verify-crumb-strip.mjs answers the session API
// requests in the browser, so the real breadcrumb components run unchanged.
export default mergeConfig(
	base,
	defineConfig({
		server: {
			fs: { allow: [searchForWorkspaceRoot(process.cwd()), realpathSync(new URL('../node_modules', import.meta.url))] },
		},
		plugins: [
			{
				name: 'crumb-strip-fixture',
				configureServer(server) {
					server.middlewares.use(async (req, res, next) => {
						// crumb-strip.html for verify-crumb-strip.mjs.
						const page = req.url?.split('?')[0].match(/^\/tests\/(crumb-strip)\.html$/)?.[1];
						if (!page || !req.url) return next();
						try {
							const html = await readFile(new URL(`./${page}.html`, import.meta.url), 'utf8');
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
