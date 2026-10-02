import { describe, expect, it } from 'vitest';
import { ENTRIES, MODULES, entriesFor, getEntry, validateEntries, wordCount, HINT_MAX_WORDS } from './registry';
import { CATEGORIES, CATEGORY_GROUPS } from './categories';
import { CATEGORY_IDS, PAGE_TYPES, type SettingEntry } from './types';

describe('settings registry', () => {
	it('has no problems: unique ids, labels, valid categories, short hints', () => {
		expect(validateEntries(ENTRIES)).toEqual([]);
	});

	it('has a module for every category and each module only holds its own entries', () => {
		for (const id of CATEGORY_IDS) {
			expect(MODULES[id]).toBeDefined();
			for (const entry of MODULES[id].entries) expect(entry.category).toBe(id);
		}
	});

	it('keeps every hint at 5 words or fewer', () => {
		for (const entry of ENTRIES) {
			if (entry.hint) expect(wordCount(entry.hint), entry.id).toBeLessThanOrEqual(HINT_MAX_WORDS);
		}
	});

	it('looks entries up by id and by place', () => {
		expect(getEntry('general.timezone')?.label).toBe('Time zone');
		expect(getEntry('nope')).toBeUndefined();
		expect(entriesFor('general').map((e) => e.id)).toContain('general.theme');
	});

	it('lists nine categories in three groups with icons', () => {
		expect(CATEGORIES.map((c) => c.id)).toEqual([...CATEGORY_IDS]);
		expect(CATEGORY_GROUPS.map((g) => g.label)).toEqual(['Personal', 'AI', 'This worker']);
		for (const c of CATEGORIES) {
			expect(c.icon).toBeTruthy();
			expect(c.type).toBe(PAGE_TYPES[c.id]);
			expect(c.label).toMatch(/^[A-Z][a-z]/);
		}
	});

	describe('validateEntries', () => {
		const ok: SettingEntry = { id: 'voice.cues', category: 'voice', section: 'Dictation', label: 'Sound cues' };

		it('flags duplicates, wrong prefixes, missing labels and long hints', () => {
			const problems = validateEntries([
				ok,
				{ ...ok },
				{ ...ok, id: 'general.cues' },
				{ ...ok, id: 'voice.nolabel', label: ' ' },
				{ ...ok, id: 'voice.long', hint: 'one two three four five six' },
				{ ...ok, id: 'voice.caps', label: 'SOUND CUES' },
				{ ...ok, id: 'voice.tab', tab: 'Usage' },
				{ ...ok, id: 'nowhere.x', category: 'nowhere' as never }
			]);
			expect(problems.join('\n')).toMatch(/voice\.cues: duplicate id/);
			expect(problems.join('\n')).toMatch(/general\.cues: id must start with "voice\."/);
			expect(problems.join('\n')).toMatch(/voice\.nolabel: missing label/);
			expect(problems.join('\n')).toMatch(/voice\.long: hint .* over 5 words/);
			expect(problems.join('\n')).toMatch(/voice\.caps: label is all caps/);
			expect(problems.join('\n')).toMatch(/voice\.tab: tab without item/);
			expect(problems.join('\n')).toMatch(/nowhere\.x: unknown category/);
		});

		it('accepts a 5-word hint', () => {
			expect(validateEntries([{ ...ok, hint: 'Applies to new calls only' }])).toEqual([]);
		});
	});
});
