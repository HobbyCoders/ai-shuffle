/** The Agents list: search, the All / Yours / Built-in filter, the folder filter and the sections. */
import { describe, expect, it } from 'vitest';
import type { Profile } from '$lib/api/client';
import {
	AGENT_ID_MAX,
	AGENT_NAME_MAX,
	FOLDER_NONE,
	duplicateAgentName,
	defaultModelBinding,
	isPinned,
	pinnedModel,
	profileModels,
	sectionAgents,
	uniqueAgentId,
	type ListQuery
} from './agentList';

const agent = (id: string, extra: Partial<Profile> = {}): Profile => ({
	id,
	name: id.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
	description: null,
	is_builtin: false,
	is_modified: false,
	config: {},
	created_at: '',
	updated_at: '',
	...extra
});

const profiles = [
	agent('juniper', { description: 'Everyday helper for the inbox' }),
	agent('release-captain', { description: 'Cuts releases' }),
	agent('scribe'),
	agent('general-purpose', { is_builtin: true, description: 'Coding agent' }),
	agent('explore', { is_builtin: true, description: 'Read-only search' })
];

const base: ListQuery = {
	query: '',
	filter: 'all',
	folder: '',
	assignments: { 'release-captain': 'Shipping', 'general-purpose': 'Shipping', scribe: 'Gone' },
	folders: ['Shipping', 'Research'],
	selectedId: null
};
const ids = (list: Profile[]) => list.map((p) => p.id);

describe('sectionAgents', () => {
	it('splits your agents and the built-ins', () => {
		const s = sectionAgents(profiles, base);
		expect(ids(s.yours)).toEqual(['juniper', 'release-captain', 'scribe']);
		expect(ids(s.builtin)).toEqual(['general-purpose', 'explore']);
		expect(s.matches).toBe(5);
	});

	it('searches name, description, id and folder', () => {
		expect(ids(sectionAgents(profiles, { ...base, query: 'cuts' }).yours)).toEqual(['release-captain']);
		expect(ids(sectionAgents(profiles, { ...base, query: 'EXPLORE' }).builtin)).toEqual(['explore']);
		const byFolder = sectionAgents(profiles, { ...base, query: 'shipping' });
		expect([...ids(byFolder.yours), ...ids(byFolder.builtin)]).toEqual(['release-captain', 'general-purpose']);
		expect(sectionAgents(profiles, { ...base, query: 'nothing like this' }).matches).toBe(0);
	});

	it('keeps the open agent listed when the search no longer matches it', () => {
		const s = sectionAgents(profiles, { ...base, query: 'cuts', selectedId: 'scribe' });
		expect(ids(s.yours)).toEqual(['release-captain', 'scribe']);
		expect(s.matches).toBe(1);
	});

	it('filters to yours or built-ins', () => {
		const yours = sectionAgents(profiles, { ...base, filter: 'yours' });
		expect(yours.builtin).toEqual([]);
		expect(ids(yours.yours)).toEqual(['juniper', 'release-captain', 'scribe']);
		const builtin = sectionAgents(profiles, { ...base, filter: 'builtin' });
		expect(builtin.yours).toEqual([]);
		expect(builtin.builtin).toHaveLength(2);
	});

	it('filters by folder, and treats a deleted folder as none', () => {
		const shipping = sectionAgents(profiles, { ...base, folder: 'Shipping' });
		expect([...ids(shipping.yours), ...ids(shipping.builtin)]).toEqual(['release-captain', 'general-purpose']);
		const none = sectionAgents(profiles, { ...base, folder: FOLDER_NONE });
		expect(ids(none.yours)).toEqual(['juniper', 'scribe']);
		expect(ids(none.builtin)).toEqual(['explore']);
	});
});

describe('models', () => {
	it('reads an agent with no provider model as using the default', () => {
		expect(isPinned({})).toBe(false);
		expect(isPinned({ __voice__: 'sonnet' })).toBe(false);
		expect(isPinned({ __native__: ' ' })).toBe(false);
		expect(isPinned({ __native__: 'opus' })).toBe(true);
	});

	it('shows native Claude first, then any provider', () => {
		expect(pinnedModel({ codex: 'prov:codex:gpt', __native__: 'opus' })).toBe('opus');
		expect(pinnedModel({ codex: 'prov:codex:gpt' })).toBe('prov:codex:gpt');
		expect(pinnedModel({ __voice__: 'x' })).toBeNull();
	});

	it('folds a legacy top-level model into the map', () => {
		expect(profileModels(agent('a', { config: { model: 'haiku' } }))).toEqual({ __native__: 'haiku' });
		expect(profileModels(agent('a', { config: { model: 'haiku', provider_models: { __native__: 'opus' } } }))).toEqual({
			__native__: 'opus'
		});
	});
});

describe('uniqueAgentId', () => {
	it('slugs the name and avoids taken ids', () => {
		expect(uniqueAgentId('Release Notes!', [])).toBe('release-notes');
		expect(uniqueAgentId('Scribe copy', ['scribe-copy', 'scribe-copy-2'])).toBe('scribe-copy-3');
		expect(uniqueAgentId('  ', [])).toBe('agent');
	});

	// #1616: the server rejects ids over 50 characters (422), so a long name never saved.
	it('cuts a long name to the 50-character id limit, suffix included', () => {
		const long = 'a'.repeat(80);
		const id = uniqueAgentId(long, []);
		expect(id).toBe('a'.repeat(AGENT_ID_MAX));
		const second = uniqueAgentId(long, [id]);
		expect(second).toBe(`${'a'.repeat(AGENT_ID_MAX - 2)}-2`);
		expect(second).toHaveLength(AGENT_ID_MAX);
		// Suffixes 2..10 taken: each base is cut so base + suffix is exactly 50.
		const suffixed = (n: number) => `${'a'.repeat(AGENT_ID_MAX - String(n).length - 1)}-${n}`;
		const taken = [id, ...[2, 3, 4, 5, 6, 7, 8, 9, 10].map(suffixed)];
		expect(taken[1]).toBe(second);
		const eleventh = uniqueAgentId(long, taken);
		expect(eleventh).toBe(`${'a'.repeat(AGENT_ID_MAX - 3)}-11`);
		expect(eleventh).toHaveLength(AGENT_ID_MAX);
	});

	it('never leaves a dash where the cut falls', () => {
		// 49 letters then a space: the slug's dash at index 49 is cut off, not kept.
		const id = uniqueAgentId(`${'b'.repeat(49)} tail words here`, []);
		expect(id).toBe('b'.repeat(49));
		expect(uniqueAgentId(`${'b'.repeat(47)} x`, [`${'b'.repeat(47)}-x`])).toBe(`${'b'.repeat(47)}-2`);
	});
});

describe('duplicateAgentName', () => {
	it('adds " copy" within the 100-character name limit', () => {
		expect(duplicateAgentName('Scribe')).toBe('Scribe copy');
		const name = duplicateAgentName('c'.repeat(AGENT_NAME_MAX));
		expect(name).toHaveLength(AGENT_NAME_MAX);
		expect(name.endsWith(' copy')).toBe(true);
	});
});

describe('defaultModelBinding', () => {
	it('pins a Claude alias on the native key', () => {
		expect(defaultModelBinding('sonnet')).toEqual({ __native__: 'sonnet' });
	});
	it('pins a provider default on its own key, never the native one', () => {
		expect(defaultModelBinding('prov:row-1:gpt-5.5')).toEqual({ 'row-1': 'prov:row-1:gpt-5.5' });
		expect(defaultModelBinding('prov:or:meta/llama:free')).toEqual({ or: 'prov:or:meta/llama:free' });
	});
	it('pins nothing without a default', () => {
		expect(defaultModelBinding('')).toEqual({});
		expect(defaultModelBinding(null)).toEqual({});
	});
});
