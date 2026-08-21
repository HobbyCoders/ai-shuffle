import { beforeEach, describe, expect, it } from 'vitest';
import {
	getFavoriteModels,
	isFavoriteModel,
	reloadFavoriteModels,
	toggleFavoriteModel
} from './modelFavorites.svelte';

beforeEach(() => {
	localStorage.clear();
	reloadFavoriteModels();
});

describe('model favorites', () => {
	it('toggles composite model values and persists their pin order', () => {
		toggleFavoriteModel('sonnet');
		toggleFavoriteModel('prov:openai:gpt-5.6-sol');

		expect(getFavoriteModels()).toEqual(['sonnet', 'prov:openai:gpt-5.6-sol']);
		expect(isFavoriteModel('sonnet')).toBe(true);
		expect(JSON.parse(localStorage.getItem('aihub.pinnedModels') ?? '[]')).toEqual([
			'sonnet',
			'prov:openai:gpt-5.6-sol'
		]);

		// Simulate a reload by rebuilding the reactive store from localStorage.
		reloadFavoriteModels();
		expect(getFavoriteModels()).toEqual(['sonnet', 'prov:openai:gpt-5.6-sol']);

		toggleFavoriteModel('sonnet');
		expect(getFavoriteModels()).toEqual(['prov:openai:gpt-5.6-sol']);
	});

	it('ignores corrupt persisted data', () => {
		localStorage.setItem('aihub.pinnedModels', '{broken');
		reloadFavoriteModels();
		expect(getFavoriteModels()).toEqual([]);
	});
});
