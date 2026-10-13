/**
 * Files store — listing, navigation races, rename/delete and uploads.
 *
 * `fetch` is stubbed with a tiny router over the /workspace endpoints. The store
 * is pessimistic: the listing only changes after the server confirms (it is
 * re-read), so a failed rename/delete has nothing to roll back and must leave
 * the listing exactly as it was.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { files, type FileItem } from './files.svelte';

function item(name: string, dir = ''): FileItem {
	const path = dir ? `${dir}/${name}` : name;
	return {
		name,
		type: 'file',
		size: 1,
		sizeFormatted: '1 B',
		path,
		extension: 'txt',
		modifiedAt: '2026-01-01T00:00:00Z',
		createdAt: '2026-01-01T00:00:00Z'
	};
}

function json(body: unknown, status = 200): Response {
	return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

type Handler = (url: URL, init: RequestInit) => Promise<Response> | Response;
let route: Handler;
let calls: { url: URL; method: string }[];

beforeEach(() => {
	files.reset();
	calls = [];
	route = () => json({ files: [] });
	vi.stubGlobal(
		'fetch',
		vi.fn(async (input: string, init: RequestInit = {}) => {
			const url = new URL(input, 'http://localhost');
			calls.push({ url, method: init.method ?? 'GET' });
			return route(url, init);
		})
	);
});

afterEach(() => {
	vi.unstubAllGlobals();
});

function listings() {
	return calls.filter((c) => c.method === 'GET' && c.url.pathname.endsWith('/workspace/files'));
}

describe('files store — listing', () => {
	it('keeps the path in the query string, so a hostile path cannot change the endpoint', async () => {
		route = () => json({ files: [item('passwd')] });
		files.navigateTo('docs/../../etc');
		await vi.waitFor(() => expect(files.current.loading).toBe(false));

		const [req] = listings();
		expect(req.url.pathname).toBe('/api/v1/workspace/files');
		expect(req.url.searchParams.get('path')).toBe('docs/../../etc');
		expect(req.url.searchParams.get('show_hidden')).toBe('false');
	});

	it('a slow listing for the previous folder never overwrites the current one', async () => {
		let releaseOld!: () => void;
		route = (url) => {
			const path = url.searchParams.get('path')!;
			if (path === 'old') {
				return new Promise((resolve) => {
					releaseOld = () => resolve(json({ files: [item('stale.txt', 'old')] }));
				});
			}
			return json({ files: [item('fresh.txt', path)] });
		};

		files.navigateTo('old');
		files.navigateTo('new');
		await vi.waitFor(() => expect(files.current.files.map((f) => f.path)).toEqual(['new/fresh.txt']));

		releaseOld();
		await new Promise((r) => setTimeout(r, 0));
		await new Promise((r) => setTimeout(r, 0));

		expect(files.current.currentPath).toBe('new');
		expect(files.current.files.map((f) => f.path)).toEqual(['new/fresh.txt']);
		expect(files.current.loading).toBe(false);
	});
});

describe('files store — rename / delete', () => {
	it('a refused rename keeps the listing and shows the reason; a non-JSON error still reads cleanly', async () => {
		route = () => json({ files: [item('a.txt')] });
		await files.loadFiles();
		const before = files.current.files;

		route = (url, init) =>
			init.method === 'PUT' ? json({ detail: 'Path escapes the workspace' }, 400) : json({ files: [item('a.txt')] });
		expect(await files.rename('a.txt', '../x.txt')).toBe(false);
		expect(files.current.error).toBe('Path escapes the workspace');
		expect(files.current.files).toBe(before);

		// A proxy error page (HTML) must not leak a JSON parse error into the UI.
		route = (url, init) =>
			init.method === 'DELETE'
				? new Response('<html>502 Bad Gateway</html>', { status: 502 })
				: json({ files: [item('a.txt')] });
		expect(await files.delete('a.txt')).toBe(false);
		expect(files.current.error).toBe('Failed to delete');
		expect(files.current.files).toBe(before);
	});

	it('deleteSelected keeps going past a failure, reports it and clears the selection', async () => {
		let remaining = ['a.txt', 'b.txt', 'c.txt'];
		route = (url, init) => {
			if (init.method === 'DELETE') {
				const path = url.searchParams.get('path')!;
				if (path === 'b.txt') return json({ detail: 'b.txt is locked' }, 409);
				remaining = remaining.filter((n) => n !== path);
				return new Response(null, { status: 204 });
			}
			return json({ files: remaining.map((n) => item(n)) });
		};
		await files.loadFiles();
		files.selectAll();
		expect(files.current.selectedFiles.size).toBe(3);

		expect(await files.deleteSelected()).toBe(false);

		const deletes = calls.filter((c) => c.method === 'DELETE').map((c) => c.url.searchParams.get('path'));
		expect(deletes).toEqual(['a.txt', 'b.txt', 'c.txt']);
		expect(files.current.files.map((f) => f.name)).toEqual(['b.txt']);
		expect(files.current.selectedFiles.size).toBe(0);
		expect(files.current.error).toBe('b.txt is locked');
	});
});

describe('files store — upload', () => {
	it('reports progress per file, stops at the first failure and still shows what landed', async () => {
		const landed: string[] = [];
		route = async (url, init) => {
			if (url.pathname.endsWith('/workspace/upload')) {
				const file = (init.body as FormData).get('file') as File;
				if (file.name === 'b.txt') return new Response('<html>502</html>', { status: 502 });
				landed.push(file.name);
				return json({ ok: true });
			}
			return json({ files: landed.map((n) => item(n, 'inbox')) });
		};
		files.navigateTo('inbox');
		await vi.waitFor(() => expect(files.current.loading).toBe(false));

		const progress: number[] = [];
		const unsub = files.subscribe((s) => {
			if (s.uploading && progress.at(-1) !== s.uploadProgress) progress.push(s.uploadProgress);
		});
		const list = [new File(['a'], 'a.txt'), new File(['b'], 'b.txt'), new File(['c'], 'c.txt')];
		const ok = await files.uploadFiles(list as unknown as FileList);
		unsub();

		expect(ok).toBe(false);
		expect(progress).toEqual([0, 33]);
		expect(landed).toEqual(['a.txt']); // c.txt was never attempted
		const uploads = calls.filter((c) => c.url.pathname.endsWith('/workspace/upload'));
		expect(uploads).toHaveLength(2);
		expect(files.current.uploading).toBe(false);
		expect(files.current.uploadProgress).toBe(0);
		expect(files.current.error).toBe('Failed to upload b.txt');
		expect(files.current.files.map((f) => f.path)).toEqual(['inbox/a.txt']);
	});
});
