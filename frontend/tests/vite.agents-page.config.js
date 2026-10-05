import { readFile } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import { defineConfig, mergeConfig, searchForWorkspaceRoot } from 'vite';
import base from '../vite.config.js';
// Serve the standalone Agents page fixture before SvelteKit's authenticated
// shell. No module stubs: agents-page.ts fakes the API inside the page, so
// the real api client and components run unchanged.
export default mergeConfig(
	base,
	defineConfig({
		server: {
			fs: { allow: [searchForWorkspaceRoot(process.cwd()), realpathSync(new URL('../node_modules', import.meta.url))] },
		},
		plugins: [
			{
				name: 'agents-page-fixture',
				configureServer(server) {
					server.middlewares.use(async (req, res, next) => {
						if (req.url?.split('?')[0] !== '/tests/agents-page.html') return next();
						try {
							const html = await readFile(new URL('./agents-page.html', import.meta.url), 'utf8');
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
