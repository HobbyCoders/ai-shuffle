// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { exportSession } from './sessions';

vi.mock('./runtime', () => ({
	apiBase: () => '/api/v1',
	withAuth: (init: RequestInit) => init
}));

const OriginalURL = URL;
const fetchMock = vi.fn();
const createURL = vi.fn(() => 'blob:test-export');
const revokeURL = vi.fn();
const downloaded: string[] = [];

function fileResponse(filename?: string, privacy?: string) {
	const headers = new Headers(filename ? { 'Content-Disposition': `attachment; filename="${filename}"` } : {});
	if (privacy) headers.set('X-Transcript-Privacy', privacy);
	return {
		ok: true,
		status: 200,
		headers,
		blob: vi.fn(async () => new Blob(['harmless fixture']))
	};
}

beforeEach(() => {
	vi.clearAllMocks();
	downloaded.length = 0;
	vi.stubGlobal('fetch', fetchMock);
	vi.stubGlobal('URL', class extends OriginalURL {
		static createObjectURL = createURL;
		static revokeObjectURL = revokeURL;
	});
	vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
		downloaded.push(this.download);
	});
});
afterEach(() => {
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

describe('sharing-safe session download boundary', () => {
	it.each([undefined, { shareSafe: false }])('keeps raw backup requests unchanged with %j', async options => {
		fetchMock.mockResolvedValueOnce(fileResponse());
		await exportSession('session-test', 'jsonl', options);
		expect(fetchMock).toHaveBeenCalledExactlyOnceWith('/api/v1/sessions/session-test/export?format=jsonl', { method: 'GET' });
		expect(downloaded).toEqual(['session-export.jsonl']);
	});

	it('opts into filtering and a distinct Markdown fallback filename', async () => {
		fetchMock.mockResolvedValueOnce(fileResponse(undefined, 'share-safe'));
		await exportSession('session-test', 'markdown', { shareSafe: true });
		expect(fetchMock).toHaveBeenCalledExactlyOnceWith('/api/v1/sessions/session-test/export?format=markdown&share_safe=true', { method: 'GET' });
		expect(downloaded).toEqual(['session-export-share-safe.md']);
		expect(revokeURL).toHaveBeenCalledExactlyOnceWith('blob:test-export');
		expect(document.body.querySelector('a')).toBeNull();
	});

	it('uses the server-provided filename on a sharing-safe response', async () => {
		fetchMock.mockResolvedValueOnce(fileResponse('chat-share-safe.md', 'share-safe'));
		await exportSession('session-test', 'markdown', { shareSafe: true });
		expect(downloaded).toEqual(['chat-share-safe.md']);
	});

	it.each([undefined, 'private-raw', 'unexpected'])('rejects successful but unconfirmed sharing responses (%s)', async privacy => {
		const response = fileResponse('raw-chat.md', privacy);
		fetchMock.mockResolvedValueOnce(response);
		await expect(exportSession('session-test', 'markdown', { shareSafe: true })).rejects.toMatchObject({
			detail: 'Server did not confirm a sharing-safe export. Nothing was downloaded.'
		});
		expect(fetchMock).toHaveBeenCalledExactlyOnceWith('/api/v1/sessions/session-test/export?format=markdown&share_safe=true', { method: 'GET' });
		expect(response.blob).not.toHaveBeenCalled();
		expect(createURL).not.toHaveBeenCalled();
		expect(downloaded).toEqual([]);
	});

	it('propagates a rejected sharing export without retrying or downloading raw history', async () => {
		fetchMock.mockResolvedValueOnce({ ok: false, status: 400, json: async () => ({ detail: 'Cannot redact malformed backup' }) });
		await expect(exportSession('session-test', 'markdown', { shareSafe: true })).rejects.toMatchObject({ status: 400, detail: 'Cannot redact malformed backup' });
		expect(fetchMock).toHaveBeenCalledExactlyOnceWith('/api/v1/sessions/session-test/export?format=markdown&share_safe=true', { method: 'GET' });
		expect(createURL).not.toHaveBeenCalled();
		expect(downloaded).toEqual([]);
	});
});
