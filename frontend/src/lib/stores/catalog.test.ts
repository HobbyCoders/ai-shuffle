import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

type CatalogStore = (typeof import('./catalog'))['catalogStore'];

describe('catalogStore.refresh', () => {
	let catalogStore: CatalogStore;
	let fetchMock: ReturnType<typeof vi.fn>;

	beforeEach(async () => {
		vi.resetModules();
		fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);
		catalogStore = (await import('./catalog')).catalogStore;
	});

	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it('surfaces the server detail when fal.ai cannot be read (502)', async () => {
		const detail = 'Could not refresh the model catalog: fal.ai timed out. Still using the 12 cached models.';
		fetchMock.mockResolvedValueOnce(Response.json(
			{ detail, success: false, error: 'fal.ai timed out', cached_models: 12, categories: [] },
			{ status: 502 },
		));

		await catalogStore.refresh();

		expect(fetchMock).toHaveBeenCalledTimes(1);
		expect(String(fetchMock.mock.calls[0][0])).toContain('/catalog/refresh');
		expect(catalogStore.error).toBe(detail);
		expect(catalogStore.loading).toBe(false);
		expect(catalogStore.loaded).toBe(false);
	});

	it('falls back to a generic message for a non-API failure', async () => {
		fetchMock.mockRejectedValueOnce({});

		await catalogStore.refresh();

		expect(catalogStore.error).toBe('Failed to refresh catalog');
	});
});
