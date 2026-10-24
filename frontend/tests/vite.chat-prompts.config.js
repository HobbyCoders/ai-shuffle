import { readFile } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { defineConfig, mergeConfig, searchForWorkspaceRoot } from 'vite';
import base from '../vite.config.js';

// Serves tests/chat-prompts.html: the in-chat question and confirmation cards
// beside the established chat surfaces, and the Attach a project sheet, on
// fixture data. Every /api request is answered here; nothing reaches a worker.
const PROJECTS = [
	{ id: 'proj-shuffle', name: 'ai-shuffle', path: 'D:/Development/ai-shuffle' },
	{ id: 'proj-cloud', name: 'hobbycoders-cloud', path: 'D:/Development/hobbycoders-cloud' },
	{ id: 'proj-evrima', name: 'evrima-server-toolkit', path: 'D:/Development/evrima-server-toolkit' },
	{ id: 'proj-site', name: 'site', path: 'D:/Development/site' },
	{ id: 'proj-notes', name: 'notes', path: 'D:/Development/notes' },
	{ id: 'proj-mods', name: 'isle-mods', path: 'D:/Development/isle-mods' },
	{ id: 'proj-lab', name: 'homelab-scripts', path: 'D:/Development/homelab-scripts' }
];
const CLUSTER = {
	projects: [
		{ project_v2_id: 'v2-remote', origin: 'git@example.com:me/unraid-stack.git', name: 'unraid-stack', local_project_id: null, eligible: [{ id: 'unraid', online: true }] }
	]
};

/** @param {string} url */
function answer(url) {
	const path = url.split('?')[0];
	if (path.endsWith('/projects')) return PROJECTS;
	if (path.endsWith('/cluster/projects')) return CLUSTER;
	if (path.endsWith('/generation/choices')) return { remembered: {}, last: {}, worker: 'gamingpc' };
	if (path.endsWith('/generation/quote')) return { ok: true, price: { currency: 'USD', unit: 'item', amount: '0.05', total: '0.05', status: 'estimated' } };
	return {};
}

export default mergeConfig(
	base,
	defineConfig({
		server: {
			proxy: {},
			fs: {
				allow: [
					searchForWorkspaceRoot(process.cwd()),
					fileURLToPath(new URL('../../mobile/src', import.meta.url)),
					realpathSync(new URL('../node_modules', import.meta.url))
				]
			}
		},
		plugins: [
			{
				name: 'chat-prompts-fixture',
				enforce: 'pre',
				configureServer(server) {
					server.middlewares.use(async (req, res, next) => {
						if (req.url?.startsWith('/api/')) {
							res.setHeader('Content-Type', 'application/json');
							res.end(JSON.stringify(answer(req.url)));
							return;
						}
						if (req.url?.split('?')[0] !== '/tests/chat-prompts.html') return next();
						try {
							const html = await readFile(new URL('./chat-prompts.html', import.meta.url), 'utf8');
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
