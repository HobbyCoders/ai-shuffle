/**
 * Every rebuild of a provider's model entries keeps the entry's saved
 * `local_settings`. PATCH replaces the whole `models` array, so a rebuild that
 * drops the field clears an owner's local tuning on an unrelated save (a
 * rename, a token replace, a model toggle).
 */
import { describe, expect, it } from 'vitest';
import type { LocalModelSettings, ProviderModelEntry } from '$lib/api/model-providers';
import { curatedEntries, passthroughModels, rowsFromFetch, rowsFromStored } from './curation';

const tuning: LocalModelSettings = {
	server: 'llama_cpp',
	preset: 'qwen3-thinking',
	sampling: { temperature: 0.6, top_p: 0.95, top_k: 20, min_p: 0, repeat_penalty: 1.1 },
	reasoning: true
};
const tuned: ProviderModelEntry = { id: 'qwen3-8b', label: 'Qwen3 8B', context_window: 32768, local_settings: tuning };
const plain: ProviderModelEntry = { id: 'plain', label: 'Plain' };

describe('local_settings survives every model-entry rebuild', () => {
	it('passthroughModels (KeyDetail rename/endpoint/token/dialect/context saves)', () => {
		const out = passthroughModels([tuned, plain]);
		expect(out[0].local_settings).toEqual(tuning);
		expect('local_settings' in out[1]).toBe(false);
	});

	it('curatedEntries (ModelCuration toggles and Use selected)', () => {
		const out = curatedEntries([
			{ ...tuned, checked: true },
			{ ...plain, checked: true }
		]);
		expect(out[0].local_settings).toEqual(tuning);
		expect('local_settings' in out[1]).toBe(false);
	});

	it('rowsFromStored then curatedEntries (a failed catalog fetch)', () => {
		expect(curatedEntries(rowsFromStored([tuned]))[0].local_settings).toEqual(tuning);
	});

	it('rowsFromFetch keeps it for catalog rows and for stored extras the catalog lacks', () => {
		const rows = rowsFromFetch([tuned, { ...tuned, id: 'gone', label: 'Gone' }], {
			source: 'upstream',
			models: [{ id: 'qwen3-8b', label: 'qwen3-8b' }, plain]
		});
		const byId = new Map(curatedEntries(rows.map((r) => ({ ...r, checked: true }))).map((m) => [m.id, m]));
		expect(byId.get('qwen3-8b')?.local_settings).toEqual(tuning);
		expect(byId.get('gone')?.local_settings).toEqual(tuning);
		expect('local_settings' in byId.get('plain')!).toBe(false);
	});

	it('an explicit null clear is not resurrected or invented', () => {
		const out = passthroughModels([{ ...plain, local_settings: null }]);
		expect(out[0].local_settings ?? null).toBeNull();
	});
});
