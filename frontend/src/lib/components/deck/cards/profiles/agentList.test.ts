/** The Agents list: search, the All / Yours / Built-in filter, the folder filter and the sections. */
import { describe, expect, it } from 'vitest';
import type { Profile } from '$lib/api/client';
import {
	FOLDER_NONE,
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
	selectedId: null,
	assistantId: 'juniper'
};
const ids = (list: Profile[]) => list.map((p) => p.id);

describe('sectionAgents', () => {
	it('splits the assistant, your agents and the built-ins', () => {
		const s = sectionAgents(profiles, base);
		expect(ids(s.assistant)).toEqual(['juniper']);
		expect(ids(s.yours)).toEqual(['release-captain', 'scribe']);
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
		expect(ids(yours.assistant)).toEqual(['juniper']);
		const builtin = sectionAgents(profiles, { ...base, filter: 'builtin' });
		expect([...builtin.assistant, ...builtin.yours]).toEqual([]);
		expect(builtin.builtin).toHaveLength(2);
	});

	it('filters by folder, and treats a deleted folder as none', () => {
		const shipping = sectionAgents(profiles, { ...base, folder: 'Shipping' });
		expect([...ids(shipping.yours), ...ids(shipping.builtin)]).toEqual(['release-captain', 'general-purpose']);
		const none = sectionAgents(profiles, { ...base, folder: FOLDER_NONE });
		expect(ids(none.yours)).toEqual(['scribe']);
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
});
