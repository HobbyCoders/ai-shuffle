import { cleanup, render } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Component } from 'svelte';

vi.mock('$lib/stores/theme.svelte', async () => {
	const { writable } = await import('svelte/store');
	return {
		theme: { setTheme: vi.fn(), setPreset: vi.fn(), current: { preference: 'system', resolved: 'dark', presetId: null } },
		themePreference: writable('system'),
		currentPresetId: writable(null),
		themePresets: []
	};
});
vi.mock('$lib/stores/timezonePreference.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { timezonePreference: { ...writable('local'), load: vi.fn(async () => {}), setTimezone: vi.fn() } };
});

import { assertCategoryCoverage, coverageLocations } from './testing/coverage';
import { PREFERENCE_PAGES, COLLECTION_LOADERS, isPreferenceCategory, type CollectionCategory } from '$lib/components/settings/pages/index';
import { CATEGORY_IDS, type CategoryId } from './types';
import { MODULES } from './registry';
import { settingsNav } from './navigation.svelte';

afterEach(cleanup);

function el(html: string): HTMLElement {
	const div = document.createElement('div');
	div.innerHTML = html;
	return div;
}

describe('assertCategoryCoverage', () => {
	// A fixed General registry, so these harness tests do not follow the real page's rows.
	const realGeneral = MODULES.general;
	beforeEach(() => {
		MODULES.general = {
			entries: [
				{ id: 'general.theme', category: 'general', section: 'Appearance', label: 'Theme' },
				{ id: 'general.themeStyle', category: 'general', section: 'Appearance', label: 'Theme style' },
				{ id: 'general.timezone', category: 'general', section: 'Region', label: 'Time zone' }
			]
		};
	});
	afterEach(() => {
		MODULES.general = realGeneral;
	});

	it('passes when every entry renders and every anchor is registered', async () => {
		await expect(
			assertCategoryCoverage('general', () =>
				el(
					'<div data-setting-anchor="general.theme"></div><div data-setting-anchor="general.themeStyle"></div><div data-setting-anchor="general.timezone"></div>'
				)
			)
		).resolves.toBeUndefined();
	});

	it('reports missing rows, unknown anchors, other categories and duplicates', async () => {
		const error = await assertCategoryCoverage('general', () =>
			el(
				'<div data-setting-anchor="general.theme"></div><div data-setting-anchor="general.theme"></div>' +
					'<div data-setting-anchor="general.ghost"></div><div data-setting-anchor="voice.cues"></div>'
			)
		).catch((e: Error) => e);
		expect(error).toBeInstanceOf(Error);
		const text = (error as Error).message;
		expect(text).toMatch(/entry "general.themeStyle" has no rendered row/);
		expect(text).toMatch(/entry "general.timezone" has no rendered row/);
		expect(text).toMatch(/anchor "general.theme" renders more than once/);
		expect(text).toMatch(/anchor "general.ghost" has no registry entry/);
		expect(text).toMatch(/anchor "voice.cues" (has no registry entry|belongs to category "voice")/);
	});

	it('honours skips', async () => {
		await expect(
			assertCategoryCoverage('general', () => el('<div data-setting-anchor="general.theme"></div>'), {
				skip: { 'general.themeStyle': 'test', 'general.timezone': 'test' }
			})
		).resolves.toBeUndefined();
	});

	it('renders each collection place that has entries', async () => {
		const before = MODULES.logs;
		MODULES.logs = {
			entries: [
				{ id: 'logs.level', category: 'logs', section: 'Filters', label: 'Level' },
				{ id: 'logs.raw', category: 'logs', section: 'Entry', label: 'Raw', item: 'l1', tab: 'Raw' }
			]
		};
		try {
			expect(coverageLocations('logs').map((l) => l.where)).toEqual([{}, { item: 'l1', tab: 'Raw' }]);
			const seen: unknown[] = [];
			await assertCategoryCoverage('logs', (where) => {
				seen.push(where);
				return el(where.item ? '<div data-setting-anchor="logs.raw"></div>' : '<div data-setting-anchor="logs.level"></div>');
			});
			expect(seen).toEqual([{}, { item: 'l1', tab: 'Raw' }]);
		} finally {
			MODULES.logs = before;
		}
	});
});

/**
 * Rows that only show in some states, which this bare render (no data, web
 * app, defaults) does not reach. Each page's own test renders them.
 */
const CONDITIONAL_ROWS: Partial<Record<CategoryId, Record<string, string>>> = {
	voice: { 'voice.hotkey': 'only while global voice input is on' },
	security: { 'security.runsAs': 'desktop app only' },
	cloud: {
		'cloud.link': 'only once the status says the account is not linked',
		'cloud.remoteWorkers': 'only once the Cloud status loaded',
		'cloud.plan': 'only on a paid plan',
		'cloud.renewal': 'only on a paid plan or with a saved plan key',
		'cloud.upgrade': 'only when not on a paid plan',
		'cloud.key': 'only in builds with a lease server',
		'cloud.deactivate': 'only with a saved plan key'
	}
};

/**
 * Collections whose entries live on items that only exist with worker data
 * assert their coverage in their own test, which loads that data and opens
 * each item and tab.
 */
const COVERED_WITH_DATA: Partial<Record<CategoryId, string>> = {
	providers: 'components/settings/providers/ProvidersCollection.test.ts'
};

describe('every category page matches the registry', () => {
	for (const category of CATEGORY_IDS) {
		if (COVERED_WITH_DATA[category]) {
			it.skip(`${category} (covered in ${COVERED_WITH_DATA[category]})`, () => {});
			continue;
		}
		it(category, async () => {
			const component: Component = isPreferenceCategory(category)
				? PREFERENCE_PAGES[category]
				: (await COLLECTION_LOADERS[category as CollectionCategory]()).default;
			await assertCategoryCoverage(
				category,
				(where) => {
					// Collections render the place being checked (f0-api.md, Conventions).
					if (!isPreferenceCategory(category)) {
						settingsNav.clearSelection(category);
						settingsNav.go({ category, ...where });
					}
					return render(component).container;
				},
				{ skip: CONDITIONAL_ROWS[category] }
			);
		});
	}
});
