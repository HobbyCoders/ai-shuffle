// Serves tests/running-tasks.html: the production RunningTasksList against
// running-tasks-stubs.ts (no worker, no network). Run from frontend/:
//   npx vite dev --config tests/vite.running-tasks.config.js --host 0.0.0.0 --port 5194 --strictPort
// then open /tests/running-tasks.html?tasks=17&swarm=1&shell=mobile&theme=vercel
import { readFile } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import { posix } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, mergeConfig, searchForWorkspaceRoot } from 'vite';
import base from '../vite.config.js';
const stubs = fileURLToPath(new URL('./running-tasks-stubs.ts', import.meta.url));
const stubbed = [
	'/src/lib/stores/active-session',
	'/src/lib/stores/swarms',
	'/src/lib/stores/usage-target',
	'/src/lib/api/sessions',
	'/src/lib/services/session-navigation',
	'/src/lib/utils/platform'
];
export default mergeConfig(
	base,
	defineConfig({
		server: {
			fs: {
				allow: [
					searchForWorkspaceRoot(process.cwd()),
					realpathSync(new URL('../node_modules', import.meta.url)),
					fileURLToPath(new URL('../../mobile/src', import.meta.url))
				]
			}
		},
		plugins: [
			{
				name: 'running-tasks-fixture',
				enforce: 'pre',
				resolveId(id, importer) {
					const from = importer?.replace(/\\/g, '/').split('?')[0] ?? '';
					if (from.endsWith('/tests/running-tasks-stubs.ts')) return null;
					let path = id.replace(/\\/g, '/').replace(/^\$lib/, '/src/lib');
					if (path.startsWith('.') && from) path = posix.join(posix.dirname(from), path);
					path = path.replace(/\.(ts|js)$/, '');
					return stubbed.some((tail) => path.endsWith(tail)) ? stubs : null;
				},
				configureServer(server) {
					server.middlewares.use(async (req, res, next) => {
						if (req.url?.split('?')[0] !== '/tests/running-tasks.html') return next();
						try {
							const html = await readFile(new URL('./running-tasks.html', import.meta.url), 'utf8');
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
