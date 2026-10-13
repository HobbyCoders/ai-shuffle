/**
 * #1610: plugin management lives in Settings. Ported from main's
 * SettingsCard.plugins test: a "Manage plugins" link (view type `plugins`)
 * opens Settings on Plugins, passing the item through, and never routes to
 * Build.
 */
import { get } from 'svelte/store';
import { describe, expect, it } from 'vitest';
import { activeView, layout, pluginsSettingsTarget } from './layout.svelte';
import { featurePanelKeyForView } from '$lib/components/deck/lazy/panels';
import { decodeTarget } from '$lib/settings/navigation.svelte';

describe('Manage plugins links', () => {
	it('open the Settings view on Plugins with the item', () => {
		layout.openFeaturePage('plugins', 'connection:gmail');
		const view = get(activeView);
		expect(view?.type).toBe('settings');
		expect(Object.fromEntries(new URLSearchParams(view?.itemId ?? ''))).toEqual({
			category: 'plugins',
			item: 'connection:gmail'
		});
	});

	it('map a bare plugin id onto its Settings item and open the list without one', () => {
		expect(new URLSearchParams(pluginsSettingsTarget('superpowers@market')).get('item')).toBe('plugin:superpowers@market');
		expect(new URLSearchParams(pluginsSettingsTarget('plugin:x')).get('item')).toBe('plugin:x');
		expect(new URLSearchParams(pluginsSettingsTarget('projects')).get('item')).toBe('projects');
		expect(new URLSearchParams(pluginsSettingsTarget('sources')).get('item')).toBe('sources');
		expect(pluginsSettingsTarget(null)).toBe('category=plugins');
		layout.openFeaturePage('plugins');
		expect(get(activeView)).toMatchObject({ type: 'settings', itemId: 'category=plugins' });
	});

	it('route a restored plugins view to the Settings panel, not Build', () => {
		expect(featurePanelKeyForView('plugins')).toBe('settings');
		expect(featurePanelKeyForView('settings')).toBe('settings');
		expect(featurePanelKeyForView('mcp')).toBe('settings');
		expect(featurePanelKeyForView('hooks')).toBe('settings');
		expect(featurePanelKeyForView('profile')).toBe('build');
	});
});

describe('deep links into the new Settings homes', () => {
	it('decode to Plugins and to Integrations > GitHub (the Assistant cards)', () => {
		expect(decodeTarget(pluginsSettingsTarget('connection:gmail'))).toEqual({ category: 'plugins', item: 'connection:gmail' });
		expect(decodeTarget(pluginsSettingsTarget(null))).toEqual({ category: 'plugins' });
		expect(decodeTarget('plugins')).toEqual({ category: 'plugins' });
		expect(decodeTarget('category=integrations&item=github')).toEqual({ category: 'integrations', item: 'github' });
	});
});
