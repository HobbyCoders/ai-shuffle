/**
 * Tests for modelFavorites — shared picker favorites with cross-device sync.
 *
 * The server preference is the source of truth once synced; localStorage is
 * the instant-load cache. A fresh account (no server value) pushes local pins
 * up so a pre-sync device seeds every other one.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: mocks }));

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
	mocks.get.mockReset();
	mocks.put.mockReset();
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
 async function start(initial: string[] | undefined) {
  const server: Record<string, unknown> = initial ? { model_favorites: initial } : {};
  mocks.get.mockImplementation(async (path: string) => path === '/preferences/all' ? {preferences:{...server}} : {value:server.model_favorites});
  mocks.put.mockImplementation(async (path: string, body: {value: unknown}) => {
   if (!path.includes('only_if_missing') || !('model_favorites' in server)) server.model_favorites=body.value;
   return {value:server.model_favorites};
  });
  const store=await freshStore(); const {preferenceSync}=await import('$lib/services/preferences');
  preferenceSync.start(); await preferenceSync.reconnect(); return {store,preferenceSync,server};
 }
 it('adopts server favorites and serializes changes through the shared coordinator', async () => {
  const {store}=await start(['opus']); expect(store.getFavoriteModels()).toEqual(['opus']);
  store.toggleFavoriteModel('opus'); for(let i=0;i<10;i++) await Promise.resolve();
  expect(mocks.put).toHaveBeenCalledWith('/preferences/model_favorites',{key:'model_favorites',value:[]});
  expect(store.getFavoriteModels()).toEqual([]);
 });
 it('atomically seeds existing local pins only when the server has none', async () => {
  localStorage.setItem('aihub.pinnedModels',JSON.stringify(['sonnet']));
  const {store}=await start(undefined); expect(store.getFavoriteModels()).toEqual(['sonnet']);
  expect(mocks.put).toHaveBeenCalledWith('/preferences/model_favorites?only_if_missing=true',{key:'model_favorites',value:['sonnet']});
 });
 it('refreshes again on reconnect rather than syncing only once per page', async () => {
  const {store,preferenceSync,server}=await start(['opus']); server.model_favorites=['haiku'];
  await preferenceSync.reconnect(); expect(store.getFavoriteModels()).toEqual(['haiku']); expect(mocks.put).not.toHaveBeenCalled();
 });
 it('resets deleted favorites without migrating stale cached pins back', async () => {
  const {store,preferenceSync,server}=await start(['opus']); delete server.model_favorites;
  await preferenceSync.refresh('model_favorites'); expect(store.getFavoriteModels()).toEqual([]); expect(mocks.put).not.toHaveBeenCalled();
 });
});
