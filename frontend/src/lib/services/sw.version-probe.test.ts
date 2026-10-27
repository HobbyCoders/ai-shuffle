/**
 * #707 review: the desktop service worker served `/_app/` cache-first, so
 * SvelteKit's `updated.check()` read a cached `version.json` and never saw a
 * new build. The probe must always reach the network.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { expect, it, vi } from 'vitest';

type FetchListener = (event: { request: Request; respondWith: (r: Promise<Response>) => void }) => void;

function loadWorker() {
	const listeners: Record<string, FetchListener> = {};
	const cacheMatch = vi.fn(async () => new Response('{"version":"cached"}'));
	const caches = { open: vi.fn(async () => ({ match: cacheMatch, put: vi.fn(), add: vi.fn() })), keys: vi.fn(async () => []) };
	const fetch = vi.fn(async () => new Response('{"version":"server"}'));
	const self = {
		location: { origin: 'https://worker.example' },
		addEventListener: (type: string, fn: FetchListener) => { listeners[type] = fn; },
		skipWaiting: vi.fn(), clients: { claim: vi.fn(), matchAll: vi.fn(async () => []) }
	};
	const source = readFileSync(resolve(__dirname, '../../../static/sw.js'), 'utf8');
	new Function('self', 'caches', 'fetch', source)(self, caches, fetch);
	return { listeners, fetch, cacheMatch };
}

async function request(path: string) {
	const sw = loadWorker();
	let answer: Promise<Response> | undefined;
	sw.listeners.fetch({ request: new Request(`https://worker.example${path}`), respondWith: (r) => { answer = r; } });
	const body = await (await answer!).text();
	return { ...sw, body };
}

it.each(['/_app/version.json', '/mobile/_app/version.json'])('answers %s from the network only', async (path) => {
	const { body, fetch, cacheMatch } = await request(path);
	expect(body).toBe('{"version":"server"}');
	expect(fetch).toHaveBeenCalledWith(expect.any(Request), { cache: 'no-store' });
	expect(cacheMatch).not.toHaveBeenCalled();
});

it('still serves hashed assets cache-first', async () => {
	const { body, fetch } = await request('/_app/immutable/chunks/abc.js');
	expect(body).toBe('{"version":"cached"}');
	expect(fetch).not.toHaveBeenCalled();
});
