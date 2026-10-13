/**
 * Files studio: listing states, folder navigation (click + keyboard), new
 * folder / rename / delete / bulk delete / upload requests, and keeping what the
 * user typed when the worker refuses.
 *
 * `fetch` is a small router over the /workspace endpoints, so the real `files`
 * store drives the component.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { files, type FileItem } from '$lib/stores/files.svelte';
import FilesStudio from './FilesStudio.svelte';

function item(name: string, type: 'file' | 'directory' = 'file', dir = ''): FileItem {
	return {
		name, type, size: 2048, sizeFormatted: '2 KB', path: dir ? `${dir}/${name}` : name,
		extension: type === 'file' ? name.split('.').pop() ?? '' : '',
		modifiedAt: new Date(Date.now() - 5 * 60_000).toISOString(), createdAt: '2026-01-01T00:00:00Z'
	} as FileItem;
}
const json = (body: unknown, status = 200) =>
	new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

type Call = { url: URL; method: string; body?: BodyInit | null };
let calls: Call[];
let listing: Record<string, FileItem[]>;
let fail: Partial<Record<string, string>>;

beforeEach(() => {
	files.reset();
	calls = [];
	fail = {};
	listing = { '': [item('docs', 'directory'), item('notes.md')], docs: [item('guide.md', 'file', 'docs')] };
	vi.stubGlobal('fetch', vi.fn(async (input: string, init: RequestInit = {}) => {
		const url = new URL(input, 'http://localhost');
		const method = init.method ?? 'GET';
		calls.push({ url, method, body: init.body });
		const key = `${method} ${url.pathname.replace(/^.*\/workspace/, '')}`;
		if (fail[key]) return json({ detail: fail[key] }, 409);
		if (key === 'GET /files') return json({ files: listing[url.searchParams.get('path') ?? ''] ?? [] });
		return json({ ok: true });
	}));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const sent = (method: string, path: string) => calls.filter((c) => c.method === method && c.url.pathname.endsWith(path));
const list = () => screen.getByRole('region', { name: 'File list' });

async function mount() {
	render(FilesStudio);
	await within(list()).findByText('notes.md');
}

describe('FilesStudio listing', () => {
	it('shows the loading state, then the empty folder state', async () => {
		listing[''] = [];
		render(FilesStudio);
		expect(screen.getByText('Loading files…')).toBeTruthy();
		expect(await screen.findByText('This folder is empty')).toBeTruthy();
	});

	it('shows a listing failure with the worker detail', async () => {
		fail['GET /files'] = 'Path is outside the workspace';
		render(FilesStudio);
		expect(await screen.findByText('Path is outside the workspace')).toBeTruthy();
	});

	it('opens a folder by click and by keyboard, and goes back via the breadcrumb', async () => {
		await mount();
		expect(within(list()).getByText('2 KB')).toBeTruthy();
		expect(within(list()).getAllByText('5m ago')).toHaveLength(2);
		const row = within(list()).getByText('docs').closest('[role="button"]') as HTMLElement;
		await fireEvent.keyDown(row, { key: 'Enter' });
		expect(await within(list()).findByText('guide.md')).toBeTruthy();
		expect(sent('GET', '/workspace/files').some((c) => c.url.searchParams.get('path') === 'docs')).toBe(true);

		const crumbs = screen.getByRole('navigation', { name: 'Breadcrumb' });
		await fireEvent.click(within(crumbs).getAllByRole('button')[0]);
		expect(await within(list()).findByText('notes.md')).toBeTruthy();

		// A file row does not navigate on Enter.
		const fileRow = within(list()).getByText('notes.md').closest('[role="button"]') as HTMLElement;
		const before = calls.length;
		await fireEvent.keyDown(fileRow, { key: 'Enter' });
		expect(calls.length).toBe(before);
	});

	it('re-reads the listing with hidden files when the switch flips', async () => {
		await mount();
		await fireEvent.click(screen.getByRole('switch', { name: 'Toggle hidden files' }));
		await waitFor(() => expect(sent('GET', '/workspace/files').some((c) => c.url.searchParams.get('show_hidden') === 'true')).toBe(true));
	});
});

describe('FilesStudio operations', () => {
	it('creates a folder in the current path from Enter, once', async () => {
		await mount();
		await fireEvent.click(screen.getByRole('button', { name: 'New folder' }));
		const input = screen.getByPlaceholderText('Folder name…');
		expect((screen.getByRole('button', { name: 'Create' }) as HTMLButtonElement).disabled).toBe(true);
		await fireEvent.input(input, { target: { value: ' reports ' } });
		await fireEvent.keyDown(input, { key: 'Enter' });
		await fireEvent.keyDown(input, { key: 'Enter' });
		await waitFor(() => expect(screen.queryByPlaceholderText('Folder name…')).toBeNull());
		const posts = sent('POST', '/workspace/files/folder');
		expect(posts).toHaveLength(1);
		expect(posts[0].url.searchParams.get('path')).toBe('');
		expect(JSON.parse(String(posts[0].body))).toEqual({ name: 'reports' });
	});

	it('keeps the folder name when the worker refuses it', async () => {
		fail['POST /files/folder'] = 'A folder with that name exists';
		await mount();
		await fireEvent.click(screen.getByRole('button', { name: 'New folder' }));
		await fireEvent.input(screen.getByPlaceholderText('Folder name…'), { target: { value: 'docs' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Create' }));
		expect(await screen.findByText('A folder with that name exists')).toBeTruthy();
		expect((screen.getByPlaceholderText('Folder name…') as HTMLInputElement).value).toBe('docs');
	});

	it('renames with the new name and skips an unchanged name', async () => {
		await mount();
		await fireEvent.click(screen.getByRole('button', { name: 'Rename notes.md' }));
		const input = screen.getByRole('textbox', { name: 'New name for notes.md' });
		await fireEvent.keyDown(input, { key: 'Enter' });
		expect(sent('PUT', '/workspace/files/rename')).toHaveLength(0);

		await fireEvent.click(screen.getByRole('button', { name: 'Rename notes.md' }));
		await fireEvent.input(screen.getByRole('textbox', { name: 'New name for notes.md' }), { target: { value: 'todo.md' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
		await waitFor(() => expect(sent('PUT', '/workspace/files/rename')).toHaveLength(1));
		const put = sent('PUT', '/workspace/files/rename')[0];
		expect(put.url.searchParams.get('path')).toBe('notes.md');
		expect(JSON.parse(String(put.body))).toEqual({ new_name: 'todo.md' });
	});

	it('keeps the rename open when the worker refuses it', async () => {
		fail['PUT /files/rename'] = 'Name already taken';
		await mount();
		await fireEvent.click(screen.getByRole('button', { name: 'Rename notes.md' }));
		await fireEvent.input(screen.getByRole('textbox', { name: 'New name for notes.md' }), { target: { value: 'docs' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
		expect(await screen.findByText('Name already taken')).toBeTruthy();
		expect((screen.getByRole('textbox', { name: 'New name for notes.md' }) as HTMLInputElement).value).toBe('docs');
	});

	it('asks before deleting and deletes only on confirm', async () => {
		await mount();
		await fireEvent.click(screen.getByRole('button', { name: 'Delete docs' }));
		expect(screen.getByText('Delete "docs"? This cannot be undone.')).toBeTruthy();
		expect(screen.getByText('Delete folder')).toBeTruthy();
		await fireEvent.click(within(screen.getByRole('dialog')).getAllByRole('button', { name: 'Cancel' })[0]);
		expect(screen.queryByRole('dialog')).toBeNull();
		expect(sent('DELETE', '/workspace/files')).toHaveLength(0);

		await fireEvent.click(screen.getByRole('button', { name: 'Delete notes.md' }));
		await fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));
		await waitFor(() => expect(sent('DELETE', '/workspace/files')).toHaveLength(1));
		expect(sent('DELETE', '/workspace/files')[0].url.searchParams.get('path')).toBe('notes.md');
	});

	it('bulk-deletes the selected items after confirmation', async () => {
		await mount();
		await fireEvent.click(screen.getByRole('checkbox', { name: 'Select all' }));
		expect(screen.getByText('2 selected')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Delete selected' }));
		expect(screen.getByText('Delete 2 selected items? This cannot be undone.')).toBeTruthy();
		await fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));
		await waitFor(() => expect(sent('DELETE', '/workspace/files')).toHaveLength(2));
		expect(sent('DELETE', '/workspace/files').map((c) => c.url.searchParams.get('path')).sort()).toEqual(['docs', 'notes.md']);
		await waitFor(() => expect(screen.queryByText(/selected$/)).toBeNull());
	});

	it('uploads dropped files into the current folder', async () => {
		await mount();
		const file = new File(['hi'], 'hello.txt', { type: 'text/plain' });
		await fireEvent.dragOver(list(), { dataTransfer: { files: [file] } });
		expect(screen.getByText('Drop files to upload')).toBeTruthy();
		await fireEvent.drop(list(), { dataTransfer: { files: [file] } });
		await waitFor(() => expect(sent('POST', '/workspace/upload')).toHaveLength(1));
		const body = sent('POST', '/workspace/upload')[0].body as FormData;
		expect((body.get('file') as File).name).toBe('hello.txt');
		expect(body.get('path')).toBe('');
		expect(screen.queryByText('Drop files to upload')).toBeNull();
	});
});
