import { readFile } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import { defineConfig, mergeConfig, searchForWorkspaceRoot } from 'vite';
import base from '../vite.config.js';
// Serve the private-access and Settings > Plugins fixtures from one server
// for verify-plugin-sign-in.mjs, before SvelteKit's authenticated shell. No
// module stubs: the script answers every API request in the browser.
const FIXTURES = ['/tests/chat-access.html', '/tests/settings-plugins.html'];
export default mergeConfig(
	base,
	defineConfig({
		server: {
			fs: { allow: [searchForWorkspaceRoot(process.cwd()), realpathSync(new URL('../node_modules', import.meta.url))] },
		},
		plugins: [
			{
				name: 'plugin-sign-in-fixtures',
				configureServer(server) {
					server.middlewares.use(async (req, res, next) => {
						const path = req.url?.split('?')[0];
						if (!path || !FIXTURES.includes(path)) return next();
						try {
							const html = await readFile(new URL(`.${path.slice('/tests'.length)}`, import.meta.url), 'utf8');
							res.setHeader('Content-Type', 'text/html');
							res.end(await server.transformIndexHtml(path, html));
						} catch (error) {
							next(error);
						}
					});
				},
			},
		],
	}),
);
