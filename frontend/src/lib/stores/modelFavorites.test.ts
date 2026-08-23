/**
 * Tests for modelFavorites — shared picker favorites with cross-device sync.
 *
 * The server preference is the source of truth once synced; localStorage is
 * the instant-load cache. A fresh account (no server value) pushes local pins
 * up so a pre-sync device seeds every other one.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	getPreference: vi.fn(),
	setPreference: vi.fn()
}));

vi.mock('$lib/api/client', () => ({
	getPreference: mocks.getPreference,
	setPreference: mocks.setPreference
}));

import {
	getFavoriteModels,
	isFavoriteModel,
	reloadFavoriteModels,
	syncFavoriteModels,
	toggleFavoriteModel
} from './modelFavorites.svelte';

beforeEach(() => {
	localStorage.clear();
	vi.resetModules();
	mocks.getPreference.mockReset();
	mocks.setPreference.mockReset();
});

async function freshStore() {
	return await import('./modelFavorites.svelte');
}

describe('model favorites — local behavior', () => {
	it('toggles composite model values and persists their pin order', async () => {
		const { toggleFavoriteModel: t, getFavoriteModels: get, reloadFavoriteModels: r, isFavoriteModel: isFav } =
			await freshStore();

		t('sonnet');
		t('prov:openai:gpt-5.6-sol');

		expect(get()).toEqual(['sonnet', 'prov:openai:gpt-5.6-sol']);
		expect(isFav('sonnet')).toBe(true);
		expect(JSON.parse(localStorage.getItem('aihub.pinnedModels') ?? '[]')).toEqual([
			'sonnet',
			'prov:openai:gpt-5.6-sol'
		]);

		// Simulate a reload by rebuilding the reactive store from localStorage.
		r();
		expect(get()).toEqual(['sonnet', 'prov:openai:gpt-5.6-sol']);

		t('sonnet');
		expect(get()).toEqual(['prov:openai:gpt-5.6-sol']);
	});

	it('ignores corrupt persisted data', async () => {
		const { reloadFavoriteModels: r, getFavoriteModels: get } = await freshStore();
		localStorage.setItem('aihub.pinnedModels', '{broken');
		r();
		expect(get()).toEqual([]);
	});
});

describe('model favorites — server sync', () => {
	it('adopts the server list on sync and writes toggles through to it', async () => {
		mocks.getPreference.mockResolvedValue({ key: 'model_favorites', value: ['opus'] });

		const { syncFavoriteModels, getFavoriteModels: get, toggleFavoriteModel: t } = await freshStore();
		await syncFavoriteModels();
		expect(get()).toEqual(['opus']);

		vi.useFakeTimers();
		t('opus'); // unstar
		await vi.runOnlyPendingTimersAsync();
		vi.useRealTimers();

		expect(mocks.setPreference).toHaveBeenCalledWith('model_favorites', []);
		expect(get()).toEqual([]);
	});

	it('pushes existing local pins up when the server has none yet', async () => {
		mocks.getPreference.mockResolvedValue(null);
		localStorage.setItem('aihub.pinnedModels', JSON.stringify(['sonnet']));

		const { syncFavoriteModels, getFavoriteModels: get } = await freshStore();
		vi.useFakeTimers();
		await syncFavoriteModels();
		await vi.runOnlyPendingTimersAsync();
		vi.useRealTimers();

		expect(get()).toEqual(['sonnet']);
		expect(mocks.setPreference).toHaveBeenCalledWith('model_favorites', ['sonnet']);
	});

	it('stays local-only until a sync succeeds (offline-safe)', async () => {
		mocks.getPreference.mockRejectedValue(new Error('offline'));

		const { syncFavoriteModels, toggleFavoriteModel: t, getFavoriteModels: get } = await freshStore();
		vi.useFakeTimers();
		await syncFavoriteModels();
		t('sonnet');
		await vi.runOnlyPendingTimersAsync();
		vi.useRealTimers();

		expect(get()).toEqual(['sonnet']);
		expect(mocks.setPreference).not.toHaveBeenCalled();
	});

	it('syncs only once per page load', async () => {
		mocks.getPreference.mockResolvedValue({ key: 'model_favorites', value: ['opus'] });
		const { syncFavoriteModels, getFavoriteModels: get } = await freshStore();
		await syncFavoriteModels();
		localStorage.setItem('aihub.pinnedModels', JSON.stringify(['haiku']));
		await syncFavoriteModels();
		expect(get()).toEqual(['opus']);
	});
});
