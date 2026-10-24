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

const PRICE = { currency: 'USD', unit: 'item', amount: '0.025', total: '0.025', status: 'estimated' };
const INCLUDED = { currency: 'USD', unit: 'item', amount: '0', total: '0', status: 'included' };
const GROK_CHOICE = { source: 'supergrok', account: 'grok-1', worker: 'gamingpc', model: 'grok-imagine-image', options: { size: '1k', format: 'jpeg' } };
const OPTIONS = [
	{
		source: 'fal', account: 'fal-ai', worker: 'gamingpc', model: 'fal-ai/flux/dev', options: { image_size: 'square', num_images: 1 },
		verified: false, price: PRICE, quick_settings: { image_size: 'square', num_images: 1 }, available: true, reason: null,
		schema: { fields: [
			{ name: 'image_size', type: 'enum', title: 'Image size', enum: ['square', 'landscape_16_9'], default: 'square' },
			{ name: 'num_images', type: 'integer', title: 'Images', minimum: 1, maximum: 4, default: 1 },
			{ name: 'seed', type: 'integer' }
		] }
	},
	{ ...GROK_CHOICE, verified: true, price: INCLUDED, quick_settings: { size: '1k', format: 'jpeg' }, available: true, reason: null,
		schema: { fields: { size: [{ value: '1k', verified: true, selectable: true }], format: [{ value: 'jpeg', verified: true, selectable: true }] } } }
];

/**
 * @param {string} url
 * @param {string} page the fixture page that asked (its Referer)
 */
function answer(url, page) {
	const path = url.split('?')[0];
	if (path.includes('/generation/options/')) return { options: OPTIONS };
	if (path.endsWith('/projects')) return PROJECTS;
	if (path.endsWith('/cluster/projects')) return CLUSTER;
	if (path.endsWith('/generation/choices')) {
		const remembered = page.includes('only=settings') ? { image: GROK_CHOICE } : {};
		return { remembered, last: {}, worker: 'gamingpc' };
	}
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
							res.end(JSON.stringify(answer(req.url, String(req.headers.referer ?? ''))));
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
