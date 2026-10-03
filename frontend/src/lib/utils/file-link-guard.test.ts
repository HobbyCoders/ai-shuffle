import { cleanup, render, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ banner: vi.fn(() => 'banner-id'), dismissLive: vi.fn() }));
vi.mock('$lib/stores/notifications', () => ({ notify: { banner: mocks.banner, dismissLive: mocks.dismissLive } }));

import AssistantMessage from '$lib/components/chat/AssistantMessage.svelte';
import { describeFileError, dispositionFilename, installFileLinkGuard, workerApiUrl } from './file-link-guard';

const REFUSED = '/api/files/by-path?path=%2Fhome%2Fowner%2Fnotes.txt';
const REPORT = '/api/files/by-path?path=%2Fwork%2Freport.zip';

let teardown: () => void = () => {};
let fetchMock: ReturnType<typeof vi.fn>;
let saved: { href: string; download: string }[];
let createObjectURL: ReturnType<typeof vi.fn>;
const realClick = HTMLAnchorElement.prototype.click;

function jsonResponse(status: number, body: unknown): Response {
	return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

/** A primary click, as a user's mouse would send it; returns the event to inspect. */
function click(target: Element, init: MouseEventInit = {}): MouseEvent {
	const event = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0, ...init });
	target.dispatchEvent(event);
	return event;
}

/** Stop jsdom from "navigating" for clicks the guard leaves alone. */
function swallowNavigation(event: Event): void {
	event.preventDefault();
}

beforeEach(() => {
	fetchMock = vi.fn();
	vi.stubGlobal('fetch', fetchMock);
	createObjectURL = vi.fn(() => 'blob:http://localhost/1');
	URL.createObjectURL = createObjectURL as unknown as typeof URL.createObjectURL;
	URL.revokeObjectURL = vi.fn() as unknown as typeof URL.revokeObjectURL;
	saved = [];
	// The blob anchor the guard clicks to save the file: record instead of downloading.
	HTMLAnchorElement.prototype.click = function (this: HTMLAnchorElement) {
		saved.push({ href: this.href, download: this.download });
	};
	mocks.banner.mockClear();
	mocks.dismissLive.mockClear();
	teardown = installFileLinkGuard();
});

afterEach(() => {
	teardown();
	document.removeEventListener('click', swallowNavigation);
	HTMLAnchorElement.prototype.click = realClick;
	vi.unstubAllGlobals();
	cleanup();
});

async function renderLink(markdown: string): Promise<HTMLAnchorElement> {
	const view = render(AssistantMessage, { content: markdown, streaming: false, sessionId: null });
	return (await waitFor(() => view.getByRole('link'))) as HTMLAnchorElement;
}

describe('chat file links never navigate the app away', () => {
	it('a refused link shows an in-app error and the page stays where it was', async () => {
		fetchMock.mockResolvedValue(jsonResponse(403, { detail: 'Access denied: file is outside workspace' }));
		const link = await renderLink(`[notes.txt](${REFUSED})`);
		const before = location.href;

		const event = click(link);

		expect(event.defaultPrevented).toBe(true);
		await waitFor(() => expect(mocks.banner).toHaveBeenCalledTimes(1));
		expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/api/files/by-path?path=%2Fhome%2Fowner%2Fnotes.txt'), expect.any(Object));
		const [message, options] = mocks.banner.mock.calls[0] as unknown as [string, Record<string, unknown>];
		expect(message).toMatch(/outside the folders AI Shuffle may share/);
		expect(options).toMatchObject({ severity: 'error', title: 'Download failed', persist: false });
		expect(saved).toEqual([]);
		expect(location.href).toBe(before);
	});

	it('a missing file and an unreachable worker each get their own plain message', async () => {
		const link = await renderLink(`[gone](${REPORT})`);
		fetchMock.mockResolvedValueOnce(jsonResponse(404, { detail: 'File not found' }));
		click(link);
		await waitFor(() => expect(mocks.banner).toHaveBeenCalledTimes(1));
		expect(mocks.banner.mock.calls[0]).toEqual([expect.stringMatching(/no longer exists/), expect.anything()]);

		fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
		click(link);
		await waitFor(() => expect(mocks.banner).toHaveBeenCalledTimes(2));
		expect(mocks.banner.mock.calls[1]).toEqual([expect.stringMatching(/Could not reach the worker/), expect.anything()]);
	});

	it('a good link downloads the file under its served name', async () => {
		fetchMock.mockResolvedValue(new Response(new Blob(['zip bytes']), {
			status: 200,
			headers: { 'content-disposition': 'attachment; filename="report.zip"' }
		}));
		const link = await renderLink(`[Report](${REPORT})`);

		const event = click(link);

		expect(event.defaultPrevented).toBe(true);
		await waitFor(() => expect(saved).toHaveLength(1));
		expect(saved[0]).toEqual({ href: 'blob:http://localhost/1', download: 'report.zip' });
		expect(createObjectURL).toHaveBeenCalledTimes(1);
		expect(mocks.banner).not.toHaveBeenCalled();
	});

	it('gets the click before the router on the app root, which would navigate the window itself', async () => {
		fetchMock.mockResolvedValue(jsonResponse(403, { detail: 'Access denied: file is outside workspace' }));
		const link = await renderLink(`[notes.txt](${REFUSED})`);
		// SvelteKit's router listens on the app root (below the document), prevents
		// default on same-origin links and then sets location itself, unless the
		// click was already handled.
		const routed: string[] = [];
		const router = (event: MouseEvent) => {
			if (event.defaultPrevented) return;
			event.preventDefault();
			routed.push((event.target as Element).closest('a')!.href);
		};
		document.body.addEventListener('click', router);
		try {
			click(link);
			await waitFor(() => expect(mocks.banner).toHaveBeenCalledTimes(1));
			expect(routed).toEqual([]);
		} finally {
			document.body.removeEventListener('click', router);
		}
	});

	it('a download button keeps its own file name', async () => {
		fetchMock.mockResolvedValue(new Response(new Blob(['png']), { status: 200 }));
		const a = document.createElement('a');
		a.href = '/api/generated-images/by-path?path=%2Fwork%2Fsunset.png';
		a.setAttribute('download', 'sunset-final.png');
		a.textContent = 'Download';
		document.body.appendChild(a);
		click(a);
		await waitFor(() => expect(saved).toHaveLength(1));
		expect(saved[0].download).toBe('sunset-final.png');
		a.remove();
	});

	it('leaves other links, new-tab clicks and clicks another handler took alone', async () => {
		document.addEventListener('click', swallowNavigation);
		const external = await renderLink('[docs](https://example.com/api/x)');
		click(external);
		const appRoute = document.createElement('a');
		appRoute.href = '/settings';
		document.body.appendChild(appRoute);
		click(appRoute);
		const file = document.createElement('a');
		file.href = REPORT;
		document.body.appendChild(file);
		click(file, { ctrlKey: true });
		file.target = '_blank';
		click(file);
		file.removeAttribute('target');
		// The Android shell's capture listener runs first and prevents default.
		const taken = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 });
		taken.preventDefault();
		file.dispatchEvent(taken);
		await Promise.resolve();
		expect(fetchMock).not.toHaveBeenCalled();
		appRoute.remove();
		file.remove();
	});

	it('the guard is gone after teardown', () => {
		teardown();
		document.addEventListener('click', swallowNavigation);
		const file = document.createElement('a');
		file.href = REPORT;
		document.body.appendChild(file);
		click(file);
		expect(fetchMock).not.toHaveBeenCalled();
		file.remove();
	});
});

describe('file link helpers', () => {
	it('only worker /api/ URLs on this page or the API origin count', () => {
		const base = 'http://worker.lan:8000/chat';
		expect(workerApiUrl('/api/files/by-path?path=x', base)?.pathname).toBe('/api/files/by-path');
		expect(workerApiUrl('http://worker.lan:8000/api/generated-images/a.png', base)).not.toBeNull();
		expect(workerApiUrl('/settings', base)).toBeNull();
		expect(workerApiUrl('/apis', base)).toBeNull();
		expect(workerApiUrl('https://example.com/api/x', base)).toBeNull();
		expect(workerApiUrl('blob:http://worker.lan:8000/1', base)).toBeNull();
		expect(workerApiUrl('javascript:alert(1)', base)).toBeNull();
	});

	it('reads the served file name, plain or encoded', () => {
		expect(dispositionFilename('attachment; filename="a b.zip"')).toBe('a b.zip');
		expect(dispositionFilename("attachment; filename*=utf-8''r%C3%A9sum%C3%A9.pdf")).toBe('résumé.pdf');
		expect(dispositionFilename('inline')).toBeNull();
		expect(dispositionFilename(null)).toBeNull();
	});

	it('explains failures in plain words', () => {
		expect(describeFileError(403, 'Access denied: file is outside workspace')).toMatch(/outside the folders/);
		expect(describeFileError(403)).toMatch(/do not have access/);
		expect(describeFileError(401)).toMatch(/Sign in again/);
		expect(describeFileError(502)).toMatch(/error 502/);
		expect(describeFileError(409, 'This worker does not own the session')).toBe('The file could not be downloaded: This worker does not own the session');
	});
});
