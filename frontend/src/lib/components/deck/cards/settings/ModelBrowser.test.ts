import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { CatalogModel } from '$lib/types/ai-models';

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({
	api,
	getErrorMessage: (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback)
}));
import ModelBrowser from './ModelBrowser.svelte';

const model = (endpoint_id: string, over: Partial<CatalogModel> = {}): CatalogModel => ({
	endpoint_id, display_name: endpoint_id, category: 'text-to-image', description: '', status: 'active',
	tags: [], tool_type: 'image_generation', output_type: 'image', ...over
});
const catalog = [
	model('fal-ai/flux-pro', { display_name: 'FLUX Pro', description: 'Sharp photoreal images', tags: ['photo'] }),
	model('fal-ai/kling-video', { display_name: 'Kling', category: 'text-to-video', tool_type: 'video_generation', output_type: 'video' }),
	model('fal-ai/odd-one', { display_name: 'Odd one', category: 'text-to-music', tool_type: null, output_type: 'audio', tags: ['Lofi'] })
];
const categories = [
	{ category: 'text-to-image', count: 1, tool_type: 'image_generation' },
	{ category: 'text-to-video', count: 1, tool_type: 'video_generation' }
];
const cards = () => screen.queryAllByRole('button').filter(b => b.classList.contains('model-card')).map(b => b.title);
const search = () => screen.getByPlaceholderText('Search 3 models...') as HTMLInputElement;

beforeEach(() => {
	vi.clearAllMocks();
	api.get.mockImplementation(async (url: string) =>
		url === '/catalog/categories' ? { categories, total_models: 3 } : { models: catalog, total: 3, catalog_size: 3 });
});
afterEach(cleanup);

describe('ModelBrowser', () => {
	it('filters by category chip (toggling it off again) and by search across name, description, id and tags', async () => {
		render(ModelBrowser);
		await waitFor(() => expect(cards()).toHaveLength(3));
		expect(screen.getByText('3 models')).toBeTruthy();
		const video = screen.getByRole('button', { name: /Text → Video \(1\)/ });
		await fireEvent.click(video);
		expect(cards()).toEqual(['fal-ai/kling-video']);
		expect(screen.getByText('(filtered)')).toBeTruthy();
		await fireEvent.click(video);
		expect(cards()).toHaveLength(3);
		for (const [q, expected] of [['photoreal', ['fal-ai/flux-pro']], ['KLING-VID', ['fal-ai/kling-video']], ['lofi', ['fal-ai/odd-one']]] as const) {
			await fireEvent.input(search(), { target: { value: q } });
			expect(cards()).toEqual(expected);
		}
		await fireEvent.input(search(), { target: { value: 'nothing-like-this' } });
		expect(screen.getByText(/No models match your search/)).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: '✕' }));
		expect(cards()).toHaveLength(3);
	});

	it('emits the model with its tool type, falling back to its output type, and marks current defaults', async () => {
		const onselect = vi.fn();
		render(ModelBrowser, { selectedModels: { video_generation: 'fal-ai/kling-video' }, onselect });
		await waitFor(() => expect(cards()).toHaveLength(3));
		const kling = screen.getByTitle('fal-ai/kling-video');
		expect(kling.classList.contains('selected')).toBe(true);
		expect(kling.textContent).toContain('✓ Default');
		expect(screen.getByTitle('fal-ai/flux-pro').textContent).not.toContain('Default');
		await fireEvent.click(screen.getByTitle('fal-ai/flux-pro'));
		expect(onselect).toHaveBeenLastCalledWith(catalog[0], 'image_generation');
		await fireEvent.click(screen.getByTitle('fal-ai/odd-one'));
		expect(onselect).toHaveBeenLastCalledWith(catalog[2], 'audio');
	});

	it('greys out models outside the selectable tool types, explains why and does not emit them', async () => {
		const onselect = vi.fn();
		render(ModelBrowser, { onselect, selectableToolTypes: ['image_generation'] });
		await waitFor(() => expect(cards()).toHaveLength(3));
		const flux = screen.getByTitle('fal-ai/flux-pro');
		expect(flux.getAttribute('aria-disabled')).toBeNull();
		expect(flux.textContent).not.toContain('No tool uses this model');
		for (const id of ['fal-ai/kling-video', 'fal-ai/odd-one']) {
			const card = screen.getByTitle(`${id} — No tool uses this model`);
			expect(card.getAttribute('aria-disabled')).toBe('true');
			expect(card.classList.contains('unusable')).toBe(true);
			expect(card.textContent).toContain('No tool uses this model');
			await fireEvent.click(card);
		}
		expect(onselect).not.toHaveBeenCalled();
		await fireEvent.click(flux);
		expect(onselect).toHaveBeenCalledExactlyOnceWith(catalog[0], 'image_generation');
	});

	it('shows a load failure with a working retry', async () => {
		api.get.mockRejectedValueOnce(new Error('catalog offline'));
		render(ModelBrowser);
		expect(await screen.findByText('catalog offline')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
		await waitFor(() => expect(cards()).toHaveLength(3));
		expect(screen.queryByText('catalog offline')).toBeNull();
	});

	it('refreshes from fal.ai and reloads the list with the active filters', async () => {
		api.post.mockResolvedValue({ success: true, models_fetched: 9, categories });
		render(ModelBrowser);
		await waitFor(() => expect(cards()).toHaveLength(3));
		await fireEvent.click(screen.getByRole('button', { name: /Text → Image \(1\)/ }));
		await fireEvent.input(search(), { target: { value: ' flux ' } });
		await fireEvent.click(screen.getByTitle('Refresh catalog from fal.ai'));
		await waitFor(() => expect(screen.getByPlaceholderText('Search 9 models...')).toBeTruthy());
		expect(api.post).toHaveBeenCalledWith('/catalog/refresh', {});
		expect(api.get).toHaveBeenLastCalledWith('/catalog/models?category=text-to-image&q=flux');
	});

	it('explains an empty catalog', async () => {
		api.get.mockImplementation(async (url: string) =>
			url === '/catalog/categories' ? { categories: [], total_models: 0 } : { models: [], total: 0, catalog_size: 0 });
		render(ModelBrowser);
		expect(await screen.findByText('No models in catalog. Click refresh to fetch from fal.ai.')).toBeTruthy();
	});
});
