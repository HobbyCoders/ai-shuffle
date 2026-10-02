import { readFile } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { defineConfig, mergeConfig, searchForWorkspaceRoot } from 'vite';
import base from '../vite.config.js';

// Canned API answers for the usage-rows fixture; every other /api call is a 404.
const defaults = { enabled: true, session_turns_per_hour: 12, install_turns_per_hour: 60, worker_max_minutes: 60 };
const API = new Map(Object.entries({
	'/api/v1/settings/loop-guard': {
		...defaults, defaults, applies: true,
		unattended_sources: ['automation', 'wakeup', 'provider_agent', 'connector'],
		usage: { install_last_hour: 3 },
		held: [{ id: 'h1', session_id: 's1', turn_id: 't1', source: 'automation', prompt: 'Nightly dependency report', reason: 'x', held_at: 1759350000 }],
	},
	'/api/v1/settings/integrations': { fal_api_key_set: true, fal_api_key_masked: 'fal-abc...wxyz', anthropic_api_key_set: false },
	'/api/v1/model-providers': [],
}));

export default mergeConfig(
	base,
	defineConfig({
		server: {
			fs: { allow: [searchForWorkspaceRoot(process.cwd()), realpathSync(new URL('../node_modules', import.meta.url))] },
		},
		plugins: [
			{
				name: 'usage-rows-fixture',
				enforce: 'pre',
				configureServer(server) {
					server.middlewares.use(async (req, res, next) => {
						const path = req.url?.split('?')[0] ?? '';
						if (path.startsWith('/api/')) {
							res.setHeader('Content-Type', 'application/json');
							if (API.has(path)) return res.end(JSON.stringify(API.get(path)));
							res.statusCode = 404;
							return res.end('{}');
						}
						if (path !== '/tests/usage-rows.html') return next();
						try {
							const html = await readFile(new URL('./usage-rows.html', import.meta.url), 'utf8');
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
