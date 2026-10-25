/** Settings hits in Spotlight: built from the Settings page's own index. */
import { describe, expect, it, vi } from 'vitest';
vi.mock('$lib/api/client', () => ({ api: { get: vi.fn() } }));
vi.mock('$lib/api/sessions', () => ({ searchSessionsPaginated: vi.fn() }));
import { settingsResults, SETTINGS_RESULT_LIMIT } from './search';
import { buildIndex, query } from '$lib/settings/search';
import type { SearchItem as SettingsSearchItem } from '$lib/settings/types';

describe('settingsResults', () => {
	it('maps a hit to a row that opens its exact setting', () => {
		const open = vi.fn();
		const rows = settingsResults('time zone', buildIndex({ desktopApp: false }), query, open);
		const row = rows.find((r) => r.label === 'Time zone');
		expect(row).toMatchObject({ label: 'Time zone', description: 'General, Region' });
		expect(row?.id).toBe('setting:Time zone\u0000General, Region');
		row?.action?.();
		expect(open).toHaveBeenCalledWith({ category: 'general', anchor: 'general.timezone' });
	});

	it('keeps the index platform filter: desktop-app rows only in the desktop app', () => {
		const web = settingsResults('agents run as you', buildIndex({ desktopApp: false }), query, vi.fn());
		const app = settingsResults('agents run as you', buildIndex({ desktopApp: true }), query, vi.fn());
		expect(web.map((r) => r.label)).not.toContain('Agents run as you');
		expect(app.map((r) => r.label)).toContain('Agents run as you');
	});

	it(`shows at most ${SETTINGS_RESULT_LIMIT} rows, in the Settings page's order`, () => {
		const index: SettingsSearchItem[] = Array.from({ length: 9 }, (_, i) => ({
			title: `Theme ${i}`,
			where: 'General',
			target: { category: 'general', anchor: `general.t${i}` }
		}));
		const rows = settingsResults('theme', index, query, vi.fn());
		expect(rows).toHaveLength(SETTINGS_RESULT_LIMIT);
		expect(rows.map((r) => r.label)).toEqual(query('theme', index).slice(0, SETTINGS_RESULT_LIMIT).map((h) => h.title));
	});

	it('returns nothing for a blank query or an index that is not loaded', () => {
		const q = vi.fn(query);
		expect(settingsResults('   ', buildIndex({ desktopApp: false }), q, vi.fn())).toEqual([]);
		expect(settingsResults('theme', [], q, vi.fn())).toEqual([]);
		expect(q).not.toHaveBeenCalled();
	});
});
