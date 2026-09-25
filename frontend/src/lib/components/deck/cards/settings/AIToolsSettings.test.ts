import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { api } from '$lib/api/client';
import { resourceSync } from '$lib/services/resource-sync';
import type { CatalogModel } from '$lib/types/ai-models';

const stt = vi.hoisted(() => ({ invalidateSttCapability: vi.fn() }));
vi.mock('$lib/services/sttStream', () => stt);
import AIToolsSettings from './AIToolsSettings.svelte';

const model = (endpoint_id: string, over: Partial<CatalogModel> = {}): CatalogModel => ({
	endpoint_id, display_name: endpoint_id, category: 'text-to-image', description: '', status: 'active',
	tags: [], tool_type: 'image_generation', output_type: 'image', ...over
});
const catalog = [
	model('fal-ai/new-image', { display_name: 'New Image' }),
	model('fal-ai/wizper', { display_name: 'Wizper', category: 'speech-to-text', tool_type: 'stt', output_type: 'text' })
];
let settings: Record<string, unknown>;
const currents = () => [...document.querySelectorAll('.tool-current')].map(e => e.textContent?.trim());

beforeEach(() => {
	vi.clearAllMocks();
	settings = { image_model: 'fal-ai/flux/dev', tts_voice: 'am_adam' };
	vi.spyOn(api, 'get').mockImplementation(async (url: string) => {
		if (url === '/settings/integrations') return settings;
		if (url === '/catalog/categories') return { categories: [{ category: 'text-to-image', count: 1, tool_type: 'image_generation' }], total_models: 2 };
		if (url.startsWith('/catalog/models')) return { models: catalog, total: 2, catalog_size: 2 };
		return {};
	});
	vi.spyOn(api, 'patch').mockResolvedValue({ success: true });
});
afterEach(() => { cleanup(); resourceSync.clear(); vi.restoreAllMocks(); });

describe('AIToolsSettings', () => {
	it('loads the saved selections, then persists a browser pick to that tool\'s endpoint and marks it default', async () => {
		render(AIToolsSettings);
		// The saved image model (not the registry default) is what the Image card shows.
		await waitFor(() => expect(currents()).toContain('Flux Dev'));
		const browse = await screen.findByRole('button', { name: /Browse All \(2\)/ });
		await fireEvent.click(browse);
		const pick = await screen.findByTitle('fal-ai/new-image');
		expect(pick.textContent).not.toContain('Default');
		await fireEvent.click(pick);
		await waitFor(() => expect(api.patch).toHaveBeenCalledWith('/settings/integrations/image/model', { model: 'fal-ai/new-image' }));
		await waitFor(() => expect(screen.getByTitle('fal-ai/new-image').textContent).toContain('✓ Default'));
		expect(stt.invalidateSttCapability).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Tools' }));
		await waitFor(() => expect(currents()).toContain('New Image'));
		expect(currents()).not.toContain('Flux Dev');
	});

	it('drops the cached speech-to-text capability when the STT model changes', async () => {
		render(AIToolsSettings);
		await fireEvent.click(await screen.findByRole('button', { name: /Browse All/ }));
		await fireEvent.click(await screen.findByTitle('fal-ai/wizper'));
		await waitFor(() => expect(stt.invalidateSttCapability).toHaveBeenCalledOnce());
		expect(api.patch).toHaveBeenCalledWith('/settings/integrations/audio/stt', { model: 'fal-ai/wizper' });
	});

	it('keeps the previous default when saving the pick fails', async () => {
		vi.mocked(api.patch).mockRejectedValueOnce(new Error('no key'));
		vi.spyOn(console, 'error').mockImplementation(() => {});
		render(AIToolsSettings);
		await fireEvent.click(await screen.findByRole('button', { name: /Browse All/ }));
		await fireEvent.click(await screen.findByTitle('fal-ai/new-image'));
		await waitFor(() => expect(api.patch).toHaveBeenCalledOnce());
		expect(screen.getByTitle('fal-ai/new-image').textContent).not.toContain('Default');
	});
});
