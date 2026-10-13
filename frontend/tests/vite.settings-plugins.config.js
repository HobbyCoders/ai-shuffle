import { readFile } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import { defineConfig, mergeConfig, searchForWorkspaceRoot } from 'vite';
import base from '../vite.config.js';
// Serve the standalone Settings > Plugins fixture before SvelteKit's authenticated
// shell. No module stubs: verify-settings-plugins.mjs answers the API
// requests in the browser, so the real api client and component run unchanged.
export default mergeConfig(
	base,
	defineConfig({
		server: {
			fs: { allow: [searchForWorkspaceRoot(process.cwd()), realpathSync(new URL('../node_modules', import.meta.url))] },
		},
		plugins: [
			{
				name: 'settings-plugins-fixture',
				configureServer(server) {
					server.middlewares.use(async (req, res, next) => {
						if (req.url?.split('?')[0] !== '/tests/settings-plugins.html') return next();
						try {
							const html = await readFile(new URL('./settings-plugins.html', import.meta.url), 'utf8');
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
