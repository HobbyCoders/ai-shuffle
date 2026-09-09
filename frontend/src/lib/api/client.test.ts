/**
 * Tests for API client
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ApiClient, recheckAuthOn401 } from './client';
import { auth } from '$lib/stores/auth.svelte';

describe('ApiClient', () => {
	let client: ApiClient;
	let fetchMock: ReturnType<typeof vi.fn<(...args: Parameters<typeof fetch>) => Promise<any>>>;

	beforeEach(() => {
		client = new ApiClient();
		fetchMock = vi.fn<(...args: Parameters<typeof fetch>) => Promise<any>>();
		global.fetch = (async (...args: Parameters<typeof fetch>) => {
			const response = await fetchMock(...args);
			// Existing lightweight fixtures expose repeatable methods. Real
			// Responses below exercise actual body-consumption semantics.
			if (response instanceof Response) return response;
			return { clone: () => response, ...response };
		}) as typeof fetch;
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
		it('preserves a real plain-text response after JSON parsing fails', async () => {
			fetchMock.mockResolvedValueOnce(new Response('upstream process unavailable', { status: 502 }));
			await expect(client.get('/error')).rejects.toEqual({ detail: 'upstream process unavailable', status: 502 });
		});
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

	// ── Phase 3.2: global 401 → fresh worker auth check ─────────────────────
	// There is no login page. A 401 re-checks the worker session so the root
	// layout can react to the refreshed auth store; nothing navigates.
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

		const statusCalls = () => fetchMock.mock.calls.filter(([url]) => url === '/api/v1/auth/status');

		beforeEach(() => {
			setPathname('/'); // default: an app page, not setup/pair
		});

		it('records a confirmed unauthenticated session after a fresh worker check', async () => {
			set401();
			fetchMock.mockResolvedValueOnce(Response.json({ authenticated: false }));
			await expect(client.get('/sessions')).rejects.toMatchObject({ status: 401 });
			await recheckAuthOn401();
			expect(auth.current.status?.authenticated).toBe(false);
			expect(window.location.assign).not.toHaveBeenCalled();
			expect(fetchMock).toHaveBeenLastCalledWith('/api/v1/auth/status', expect.objectContaining({
				credentials: 'include', cache: 'no-store'
			}));
		});

		it('preserves worker auth on a provider-specific 401 without replaying the operation', async () => {
			fetchMock.mockResolvedValueOnce(Response.json({ detail: 'GitHub CLI not authenticated' }, { status: 401 }));
			fetchMock.mockResolvedValueOnce(Response.json({ authenticated: true }));
			await expect(client.post('/github/create-pr', { title: 'test' })).rejects.toMatchObject({
				status: 401, detail: 'GitHub CLI not authenticated'
			});
			await recheckAuthOn401();
			expect(auth.current.status?.authenticated).toBe(true);
			expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(['/api/v1/github/create-pr', '/api/v1/auth/status']);
		});

		it('ignores a delayed old-cookie 401 after the current cookie becomes authenticated', async () => {
			let finishOld!: (response: Response) => void;
			fetchMock.mockReturnValueOnce(new Promise<Response>((resolve) => { finishOld = resolve; }));
			const oldRequest = client.get('/sessions');
			fetchMock.mockResolvedValueOnce(Response.json({ authenticated: true }));
			await auth.checkAuth(); // A new cookie has already been accepted.
			fetchMock.mockResolvedValueOnce(Response.json({ authenticated: true }));
			const rejected = expect(oldRequest).rejects.toMatchObject({ status: 401 });
			finishOld(Response.json({ detail: 'Expired old session' }, { status: 401 }));
			await rejected;
			await recheckAuthOn401();
			expect(auth.current.status?.authenticated).toBe(true);
		});

		it.each(['503', 'network'])('keeps the prior status and reports the failed fresh check (%s)', async (failure) => {
			// An older anonymous status must not substitute for the failed check.
			fetchMock.mockResolvedValueOnce(Response.json({ authenticated: false }));
			await auth.checkAuth();
			set401();
			if (failure === '503') fetchMock.mockResolvedValueOnce(Response.json({ detail: 'Hub unavailable' }, { status: 503 }));
			else fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
			await expect(client.get('/sessions')).rejects.toMatchObject({ status: 401 });
			await recheckAuthOn401();
			expect(auth.current.error).toBe('Failed to check auth status');
			expect(window.location.assign).not.toHaveBeenCalled();
		});

		it('deduplicates concurrent HTTP and socket failures into one current auth check', async () => {
			let finishStatus!: (response: Response) => void;
			fetchMock.mockImplementation((url) => url === '/api/v1/auth/status'
				? new Promise<Response>((resolve) => { finishStatus = resolve; })
				: Promise.resolve(Response.json({ detail: 'Expired' }, { status: 401 })));
			await Promise.all([
				expect(client.get('/sessions')).rejects.toMatchObject({ status: 401 }),
				expect(client.get('/projects')).rejects.toMatchObject({ status: 401 })
			]);
			const socketFailure = recheckAuthOn401();
			await vi.waitFor(() => expect(finishStatus).toBeTypeOf('function'));
			expect(statusCalls()).toHaveLength(1);
			finishStatus(Response.json({ authenticated: false }));
			await socketFailure;
			expect(statusCalls()).toHaveLength(1);
			expect(auth.current.status?.authenticated).toBe(false);
		});

		it('lets a newer successful auth check overtake an older anonymous result', async () => {
			let finishStatus!: (response: Response) => void;
			fetchMock.mockReturnValueOnce(new Promise<Response>((resolve) => { finishStatus = resolve; }));
			const confirmation = recheckAuthOn401();
			await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
			fetchMock.mockResolvedValueOnce(Response.json({ authenticated: true }));
			await auth.checkAuth();
			finishStatus(Response.json({ authenticated: false }));
			await confirmation;
			expect(auth.current.status?.authenticated).toBe(true);
		});

		// Give any (erroneously scheduled) dynamic-import re-check a real chance to
		// run before asserting it did NOT fire — otherwise "not called" is trivial.
		async function flush() {
			await new Promise((r) => setTimeout(r, 0));
			await new Promise((r) => setTimeout(r, 0));
		}

		it('does NOT re-check on a 401 from an auth endpoint (/auth/*)', async () => {
			set401();
			await expect(client.get('/auth/status')).rejects.toMatchObject({ status: 401 });
			await flush();
			expect(fetchMock).toHaveBeenCalledTimes(1);
		});

		it('does NOT re-check on a 401 from a /workspace/* probe endpoint', async () => {
			set401();
			await expect(client.get('/workspace/validate')).rejects.toMatchObject({ status: 401 });
			await flush();
			expect(fetchMock).toHaveBeenCalledTimes(1);
		});

		it('does NOT re-check when already on /setup (loop guard)', async () => {
			setPathname('/setup');
			set401();
			await expect(client.get('/sessions')).rejects.toMatchObject({ status: 401 });
			await flush();
			expect(fetchMock).toHaveBeenCalledTimes(1);
		});

		it('does NOT re-check when already on /pair (loop guard)', async () => {
			setPathname('/pair');
			set401();
			await expect(client.get('/sessions')).rejects.toMatchObject({ status: 401 });
			await flush();
			expect(fetchMock).toHaveBeenCalledTimes(1);
		});

		it('does NOT re-check on a non-401 error from a non-auth endpoint', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: false,
				status: 403,
				json: () => Promise.resolve({ detail: 'Forbidden' })
			});
			await expect(client.get('/sessions')).rejects.toMatchObject({ status: 403 });
			await flush();
			expect(fetchMock).toHaveBeenCalledTimes(1);
		});
	});
});
