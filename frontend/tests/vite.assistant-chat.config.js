import { readFile } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import { defineConfig, mergeConfig, searchForWorkspaceRoot } from 'vite';
import base from '../vite.config.js';
// Serve the assistant-chat fixture before SvelteKit's authenticated application shell.
// Run: node tests/verify-assistant-chat.mjs (it starts and stops this server itself).
export default mergeConfig(
	base,
	defineConfig({
		server: {
			fs: {
				allow: [
					searchForWorkspaceRoot(process.cwd()),
					realpathSync(new URL('../node_modules', import.meta.url)),
					// The phone shell's sources (?shell=mobile).
					realpathSync(new URL('../../mobile/src', import.meta.url)),
				],
			},
		},
		plugins: [
			{
				name: 'assistant-chat-fixture',
				enforce: 'pre',
				configureServer(server) {
					server.middlewares.use(async (req, res, next) => {
						if (req.url?.split('?')[0] !== '/tests/assistant-chat.html')
							return next();
						try {
							const html = await readFile(
								new URL('./assistant-chat.html', import.meta.url),
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
