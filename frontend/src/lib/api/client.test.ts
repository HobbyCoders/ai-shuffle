/**
 * Tests for API client
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ApiClient } from './client';

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
		it('should handle unknown error when json parsing fails', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: false,
				status: 500,
				json: () => Promise.reject(new Error('Invalid JSON'))
			});

			await expect(client.get('/error')).rejects.toEqual({
				detail: 'Unknown error',
				status: 500
			});
		});

		it('should use default message when detail is missing', async () => {
			fetchMock.mockResolvedValueOnce({
				ok: false,
				status: 400,
				json: () => Promise.resolve({})
			});

			await expect(client.get('/error')).rejects.toEqual({
				detail: 'Request failed',
				status: 400
			});
		});
	});
});
