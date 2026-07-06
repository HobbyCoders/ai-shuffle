/**
 * Tests for API client
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ApiClient } from './client';

// The 401 interceptor dynamically imports `$app/navigation` and calls goto().
// Mock it so we can assert the redirect fires (or doesn't) without a real router.
const gotoMock = vi.fn(async (_url?: string | URL) => {});
vi.mock('$app/navigation', () => ({
	goto: (url?: string | URL) => gotoMock(url)
}));

describe('ApiClient', () => {
	let client: ApiClient;
	let fetchMock: ReturnType<typeof vi.fn>;

	beforeEach(() => {
		client = new ApiClient();
		fetchMock = vi.fn();
		global.fetch = fetchMock as unknown as typeof fetch;
	});

	afterEach(() => {
		vi.restoreAllMocks();
	});

	describe('get', () => {
		it('should make GET request with correct path', async () => {
			const mockResponse = { data: 'test' };
			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockResponse)
			});

			const result = await client.get('/test');

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/test', {
				method: 'GET',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: undefined
			});
			expect(result).toEqual(mockResponse);
		});

		it('should throw ApiError on failure', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: false,
				status: 404,
				json: () => Promise.resolve({ detail: 'Not found' })
			});

			await expect(client.get('/missing')).rejects.toEqual({
				detail: 'Not found',
				status: 404
			});
		});
	});

	describe('post', () => {
		it('should make POST request with body', async () => {
			const mockResponse = { id: 1 };
			const requestBody = { name: 'test' };

			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockResponse)
			});

			const result = await client.post('/items', requestBody);

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/items', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify(requestBody)
			});
			expect(result).toEqual(mockResponse);
		});
	});

	describe('put', () => {
		it('should make PUT request with body', async () => {
			const mockResponse = { updated: true };
			const requestBody = { name: 'updated' };

			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockResponse)
			});

			const result = await client.put('/items/1', requestBody);

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/items/1', {
				method: 'PUT',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify(requestBody)
			});
			expect(result).toEqual(mockResponse);
		});
	});

	describe('patch', () => {
		it('should make PATCH request with body', async () => {
			const mockResponse = { patched: true };
			const requestBody = { field: 'value' };

			fetchMock.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockResponse)
			});

			const result = await client.patch('/items/1', requestBody);

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/items/1', {
				method: 'PATCH',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify(requestBody)
			});
			expect(result).toEqual(mockResponse);
		});
	});

	describe('delete', () => {
		it('should make DELETE request', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: true,
				status: 200,
				json: () => Promise.resolve({ deleted: true })
			});

			const result = await client.delete('/items/1');

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/items/1', {
				method: 'DELETE',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include'
			});
			expect(result).toEqual({ deleted: true });
		});

		it('should handle 204 No Content', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: true,
				status: 204
			});

			const result = await client.delete('/items/1');

			expect(result).toBeUndefined();
		});

		it('should throw ApiError on failure', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: false,
				status: 403,
				json: () => Promise.resolve({ detail: 'Forbidden' })
			});

			await expect(client.delete('/items/1')).rejects.toEqual({
				detail: 'Forbidden',
				status: 403
			});
		});
	});

	describe('error handling', () => {
		it('should fall back to text body when json parsing fails', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: false,
				status: 500,
				statusText: 'Internal Server Error',
				json: () => Promise.reject(new Error('Invalid JSON')),
				text: () => Promise.resolve('git worktree add failed')
			});

			await expect(client.get('/error')).rejects.toEqual({
				detail: 'git worktree add failed',
				status: 500
			});
		});

		it('should use status-based message when both json and text fail', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: false,
				status: 500,
				statusText: 'Internal Server Error',
				json: () => Promise.reject(new Error('Invalid JSON')),
				text: () => Promise.reject(new Error('Read failed'))
			});

			await expect(client.get('/error')).rejects.toEqual({
				detail: 'Internal server error — something went wrong on the server',
				status: 500
			});
		});

		it('should provide descriptive error for common HTTP status codes', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: false,
				status: 404,
				statusText: 'Not Found',
				json: () => Promise.reject(new Error('Invalid JSON')),
				text: () => Promise.resolve('')
			});

			await expect(client.get('/error')).rejects.toEqual({
				detail: 'Not found — the requested resource doesn\'t exist',
				status: 404
			});
		});

		it('should extract message field from json if detail is missing', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: false,
				status: 400,
				json: () => Promise.resolve({ message: 'Branch name is required' })
			});

			await expect(client.get('/error')).rejects.toEqual({
				detail: 'Branch name is required',
				status: 400
			});
		});

		it('should extract error field from json if detail and message are missing', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: false,
				status: 422,
				json: () => Promise.resolve({ error: 'Validation failed' })
			});

			await expect(client.get('/error')).rejects.toEqual({
				detail: 'Validation failed',
				status: 422
			});
		});

		it('should use status description for unknown status when body is empty', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: false,
				status: 418,
				statusText: 'I\'m a teapot',
				json: () => Promise.reject(new Error('no json')),
				text: () => Promise.resolve('')
			});

			await expect(client.get('/error')).rejects.toEqual({
				detail: 'Server error (HTTP 418: I\'m a teapot)',
				status: 418
			});
		});
	});

	// ── Phase 3.2: global 401 → /login interceptor ─────────────────────────
	describe('401 interceptor', () => {
		function set401(): void {
			fetchMock.mockResolvedValueOnce({
				ok: false,
				status: 401,
				json: () => Promise.resolve({ detail: 'Authentication required' })
			});
		}

		function setPathname(path: string): void {
			Object.defineProperty(window, 'location', {
				configurable: true,
				value: { pathname: path, assign: vi.fn() }
			});
		}

		beforeEach(() => {
			gotoMock.mockClear();
			setPathname('/'); // default: an app page, not login/setup
		});

		it('redirects to /login on a 401 from a NON-auth endpoint', async () => {
			set401();
			await expect(client.get('/sessions')).rejects.toMatchObject({ status: 401 });
			// goto fires via a dynamic import() → poll until the async chain resolves.
			await vi.waitFor(() => expect(gotoMock).toHaveBeenCalledWith('/login'));
		});

		// Give any (erroneously scheduled) dynamic-import redirect a real chance to
		// run before asserting it did NOT fire — otherwise "not called" is trivial.
		async function flush() {
			await new Promise((r) => setTimeout(r, 0));
			await new Promise((r) => setTimeout(r, 0));
		}

		it('does NOT redirect on a 401 from an auth endpoint (/auth/*)', async () => {
			set401();
			await expect(client.get('/auth/status')).rejects.toMatchObject({ status: 401 });
			await flush();
			expect(gotoMock).not.toHaveBeenCalled();
		});

		it('does NOT redirect on a 401 from the /auth/login endpoint', async () => {
			set401();
			await expect(client.post('/auth/login', { u: 'x' })).rejects.toMatchObject({ status: 401 });
			await flush();
			expect(gotoMock).not.toHaveBeenCalled();
		});

		it('does NOT redirect on a 401 from a /workspace/* probe endpoint', async () => {
			set401();
			await expect(client.get('/workspace/validate')).rejects.toMatchObject({ status: 401 });
			await flush();
			expect(gotoMock).not.toHaveBeenCalled();
		});

		it('does NOT redirect when already on /login (loop guard)', async () => {
			setPathname('/login');
			set401();
			await expect(client.get('/sessions')).rejects.toMatchObject({ status: 401 });
			await flush();
			expect(gotoMock).not.toHaveBeenCalled();
		});

		it('does NOT redirect when already on /setup (loop guard)', async () => {
			setPathname('/setup');
			set401();
			await expect(client.get('/sessions')).rejects.toMatchObject({ status: 401 });
			await flush();
			expect(gotoMock).not.toHaveBeenCalled();
		});

		it('does NOT redirect on a non-401 error from a non-auth endpoint', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: false,
				status: 403,
				json: () => Promise.resolve({ detail: 'Forbidden' })
			});
			await expect(client.get('/sessions')).rejects.toMatchObject({ status: 403 });
			await flush();
			expect(gotoMock).not.toHaveBeenCalled();
		});
	});
});
