/**
 * Files page against another of the owner's workers: the worker chip and its
 * menu, routing every request to the chosen worker, the loading state that
 * never shows the previous worker's rows, and the offline / refused states.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { files, filesBaseFor, type FileItem } from '$lib/stores/files.svelte';
import type { Install } from '$lib/api/installs';
import FilesStudio from './FilesStudio.svelte';
import { workerOptions } from './files-workers';

function item(name: string, type: 'file' | 'directory' = 'file'): FileItem {
	return {
		name, type, size: 10, sizeFormatted: '10 B', path: name, extension: '',
		modifiedAt: new Date().toISOString(), createdAt: '2026-01-01T00:00:00Z'
	} as FileItem;
}
function install(over: Partial<Install>): Install {
	return {
		id: 'x', device_name: 'x', base_url: null, platform: 'linux', version: '1', capabilities: {},
		last_seen: null, online: true, approved: true, ...over
	};
}
const json = (body: unknown, status = 200) =>
	new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const INSTALLS: Install[] = [
	install({ id: 'dev_self', device_id: 'dev_self', device_name: 'ServerWorker', is_self: true }),
	install({ id: 'dev_pc', device_id: 'dev_pc', device_name: 'DesktopWorker', platform: 'windows' }),
	install({ id: 'dev_lap', device_id: 'dev_lap', device_name: 'Laptop', online: false })
];

let calls: { url: URL; method: string }[];
let installs: Install[];
let remote: { status: number; body: unknown } | 'hang';
let held: (() => void)[];
const release = () => held.splice(0).forEach((r) => r());

beforeEach(() => {
	files.reset();
	localStorage.clear();
	calls = [];
	held = [];
	installs = INSTALLS;
	remote = { status: 200, body: { files: [item('game-saves', 'directory'), item('far.txt')] } };
	vi.stubGlobal('fetch', vi.fn(async (input: string, init: RequestInit = {}) => {
		const url = new URL(input, 'http://localhost');
		const method = init.method ?? 'GET';
		calls.push({ url, method });
		if (url.pathname.endsWith('/cluster/installs')) return json({ installs, self_instance_id: 'dev_self' });
		if (url.pathname.includes('/workspace/workers/')) {
			if (remote === 'hang') await new Promise<void>((r) => { held.push(r); });
			const r = remote === 'hang' ? { status: 200, body: { files: [item('far.txt')] } } : remote;
			return json(r.body, r.status);
		}
		if (url.pathname.endsWith('/workspace/files')) return json({ files: [item('docs', 'directory'), item('notes.md')] });
		return json({ ok: true });
	}));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); });

const list = () => screen.getByRole('region', { name: 'File list' });
const chip = () => screen.getByRole('button', { name: /^Worker:/ });

async function openMenu() {
	await fireEvent.click(chip());
	return screen.getByRole('menu', { name: 'Show files from' });
}

describe('worker options', () => {
	it('puts this device first, keys peers by device id, disables offline ones and leaves out unapproved ones', () => {
		const options = workerOptions([
			...INSTALLS,
			install({ id: 'dev_new', device_id: 'dev_new', device_name: 'NewBox', approved: false }),
			install({ id: 'dev_gone', device_id: 'dev_gone', device_name: 'Gone', revoked_at: '2026-01-01' })
		]);
		expect(options.map((o) => [o.key, o.name, o.disabled, o.reason])).toEqual([
			[null, 'ServerWorker', false, null],
			['dev_pc', 'DesktopWorker', false, null],
			['dev_lap', 'Laptop', true, 'Offline']
		]);
	});

	it('tells two workers with the same name apart', () => {
		const options = workerOptions([
			install({ id: 'a', device_name: 'Box', is_self: true }),
			install({ id: 'dev_b111', device_id: 'dev_b111', device_name: 'Box', platform: 'windows' }),
			install({ id: 'dev_c222', device_id: 'dev_c222', device_name: 'box', platform: 'linux' }),
			install({ id: 'dev_d333', device_id: 'dev_d333', device_name: 'Box', platform: 'linux' })
		]);
		expect(options.map((o) => o.hint)).toEqual(['this device', 'windows', '…c222', '…d333']);
	});
});

describe('Files worker picker', () => {
	it('starts on this device and lists every worker with its state', async () => {
		render(FilesStudio);
		await within(list()).findByText('notes.md');
		await waitFor(() => expect(chip().getAttribute('aria-label')).toContain('ServerWorker'));
		const menu = await openMenu();
		const items = within(menu).getAllByRole('menuitemradio');
		expect(items.map((i) => i.textContent?.replace(/\s+/g, ' ').trim())).toEqual([
			'ServerWorker This device', 'DesktopWorker Online', 'Laptop Offline'
		]);
		expect(items[0].getAttribute('aria-checked')).toBe('true');
		expect(items[2].getAttribute('aria-disabled')).toBe('true');
		// An offline worker cannot be chosen.
		await fireEvent.click(items[2]);
		expect(calls.some((c) => c.url.pathname.includes('/workers/'))).toBe(false);
		expect(localStorage.getItem('files_worker')).toBeNull();
	});

	it('switches to another worker without showing the old rows, then lists its files', async () => {
		render(FilesStudio);
		await within(list()).findByText('notes.md');
		await waitFor(() => expect(chip().getAttribute('aria-label')).toContain('ServerWorker'));
		remote = 'hang';
		const menu = await openMenu();
		await fireEvent.click(within(menu).getByRole('menuitemradio', { name: /DesktopWorker/ }));

		expect(await within(list()).findByText('Loading files from DesktopWorker…')).toBeTruthy();
		expect(within(list()).queryByText('notes.md')).toBeNull();
		expect(chip().getAttribute('aria-label')).toContain('DesktopWorker');
		expect(localStorage.getItem('files_worker')).toBe('dev_pc');

		release();
		expect(await within(list()).findByText('far.txt')).toBeTruthy();
		const remoteCalls = calls.filter((c) => c.url.pathname.includes('/workers/'));
		expect(remoteCalls.length).toBeGreaterThan(0);
		expect(remoteCalls.every((c) => c.url.pathname.startsWith('/api/v1/workspace/workers/dev_pc/files'))).toBe(true);
		expect(screen.getByText(/manage files on DesktopWorker/)).toBeTruthy();
		expect(screen.queryByRole('button', { name: /Open terminal/ })).toBeNull();
	});

	it('shows an offline worker in place of the list and goes back to this device', async () => {
		files.setWorker('dev_pc');
		remote = { status: 503, body: { detail: 'That worker is offline or unreachable.' } };
		render(FilesStudio);

		expect(await within(list()).findByText('DesktopWorker is offline')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'New folder' }).hasAttribute('disabled')).toBe(true);
		expect(screen.getByRole('button', { name: 'Upload' }).hasAttribute('disabled')).toBe(true);

		await fireEvent.click(within(list()).getByRole('button', { name: 'Back to this device' }));
		expect(await within(list()).findByText('notes.md')).toBeTruthy();
		expect(localStorage.getItem('files_worker')).toBeNull();
	});

	it('shows the far worker\'s refusal as-is, not as offline', async () => {
		files.setWorker('dev_pc');
		remote = { status: 400, body: { detail: 'Path escapes workspace boundary' } };
		render(FilesStudio);
		expect(await within(list()).findByText('Path escapes workspace boundary')).toBeTruthy();
		expect(within(list()).getByText('This folder could not be opened')).toBeTruthy();
		expect(within(list()).queryByText(/is offline/)).toBeNull();
	});

	it('offers a way back to this device when the other worker refuses the list', async () => {
		files.setWorker('dev_pc');
		remote = { status: 403, body: { detail: 'Not allowed' } };
		render(FilesStudio);
		expect(await within(list()).findByText('Not allowed')).toBeTruthy();
		await fireEvent.click(within(list()).getByRole('button', { name: 'Back to this device' }));
		expect(await within(list()).findByText('notes.md')).toBeTruthy();
		expect(localStorage.getItem('files_worker')).toBeNull();
	});

	it('does not offer Back to this device for an error on this device', async () => {
		const real = fetch;
		vi.stubGlobal('fetch', async (input: string, init: RequestInit = {}) =>
			new URL(input, 'http://localhost').pathname.endsWith('/workspace/files')
				? json({ detail: 'Not allowed' }, 403)
				: real(input, init));
		render(FilesStudio);
		expect(await within(list()).findByText('Not allowed')).toBeTruthy();
		expect(within(list()).queryByRole('button', { name: 'Back to this device' })).toBeNull();
	});

	it('falls back to this device when the remembered worker is no longer paired', async () => {
		files.setWorker('dev_departed');
		render(FilesStudio);
		await waitFor(() => expect(files.current.workerId).toBeNull());
		expect(await within(list()).findByText('notes.md')).toBeTruthy();
		expect(localStorage.getItem('files_worker')).toBeNull();
	});

	it('tolerates a worker list it cannot read', async () => {
		installs = undefined as unknown as Install[];
		render(FilesStudio);
		await within(list()).findByText('notes.md');
		const menu = await openMenu();
		expect(within(menu).getAllByRole('menuitemradio')).toHaveLength(1);
		expect(within(menu).getByText('No other workers in this network')).toBeTruthy();
	});
});

describe('files store worker routing', () => {
	it('sends every operation to the selected worker', async () => {
		expect(filesBaseFor(null)).toMatch(/\/api\/v1\/workspace$/);
		expect(filesBaseFor('dev pc')).toMatch(/\/api\/v1\/workspace\/workers\/dev%20pc$/);
		files.setWorker('dev_pc');
		await waitFor(() => expect(files.current.loading).toBe(false));
		await files.createFolder('new');
		await files.rename('far.txt', 'near.txt');
		await files.delete('far.txt');
		const writes = calls.filter((c) => c.method !== 'GET' && !c.url.pathname.endsWith('/cluster/installs'));
		expect(writes.map((c) => `${c.method} ${c.url.pathname}`)).toEqual([
			'POST /api/v1/workspace/workers/dev_pc/files/folder',
			'PUT /api/v1/workspace/workers/dev_pc/files/rename',
			'DELETE /api/v1/workspace/workers/dev_pc/files'
		]);
	});
});
