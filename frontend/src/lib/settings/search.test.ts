import { afterEach, describe, expect, it } from 'vitest';
import { buildIndex, query, MAX_RESULTS } from './search';
import { MODULES } from './registry';
import type { CategoryModule, SearchItem } from './types';

const original = { ...MODULES };
afterEach(() => {
	Object.assign(MODULES, original);
});

function item(title: string, where = 'General', keywords?: string): SearchItem {
	return { title, where, keywords, target: { category: 'general' } };
}

describe('settings search index', () => {
	it('holds every category and every registry entry', () => {
		const index = buildIndex({ desktopApp: false });
		expect(index.find((i) => i.title === 'Default models')?.target).toEqual({ category: 'models' });
		const tz = index.find((i) => i.title === 'Time zone');
		expect(tz).toMatchObject({ where: 'General, Region', target: { category: 'general', anchor: 'general.timezone' } });
	});

	it('includes dynamic items from a category searchSource', () => {
		MODULES.providers = {
			entries: [],
			searchSource: () => [{ title: 'Claude Max subscription', where: 'Providers, Claude', target: { category: 'providers', item: 'c-sub' } }]
		} satisfies CategoryModule;
		expect(query('claude max', buildIndex())[0].target).toEqual({ category: 'providers', item: 'c-sub' });
	});

	it('survives a failing searchSource', () => {
		MODULES.logs = {
			entries: [],
			searchSource: () => {
				throw new Error('store not ready');
			}
		};
		expect(() => buildIndex()).not.toThrow();
	});

	it('leaves desktop-app entries out of the web UI', () => {
		MODULES.security = {
			entries: [{ id: 'security.runsAs', category: 'security', section: 'Agent safety', label: 'Agents run as you', platform: 'desktop-app' }]
		};
		expect(buildIndex({ desktopApp: false }).some((i) => i.title === 'Agents run as you')).toBe(false);
		expect(buildIndex({ desktopApp: true }).some((i) => i.title === 'Agents run as you')).toBe(true);
	});
});

describe('settings search query', () => {
	it('finds a setting by keyword and by title', () => {
		expect(query('timezone')[0].title).toBe('Time zone');
		expect(query('dark mode')[0].title).toBe('Theme');
	});

	it('requires every word to match somewhere', () => {
		const index = [item('Loop guard', 'Providers, Claude, Policy', 'unattended turns'), item('Held turns', 'Providers')];
		expect(query('loop turns', index).map((r) => r.title)).toEqual(['Loop guard']);
		expect(query('loop nothing', index)).toEqual([]);
	});

	it('ranks titles that start with the first word first', () => {
		const index = [item('Rotate API key', 'Integrations'), item('Key rotation reminder'), item('API key', 'Providers, OpenAI')];
		expect(query('key', index).map((r) => r.title)).toEqual(['Key rotation reminder', 'Rotate API key', 'API key']);
	});

	it('drops duplicates and caps the list', () => {
		const index = [item('Theme'), item('Theme'), ...Array.from({ length: 30 }, (_, i) => item(`Thing ${i}`))];
		expect(query('theme', index)).toHaveLength(1);
		expect(query('thing', index)).toHaveLength(MAX_RESULTS);
	});

	it('returns nothing for a blank query', () => {
		expect(query('   ')).toEqual([]);
	});
});
