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
		expect(pluginsSettingsTarget(null)).toBe('category=plugins');
		layout.openFeaturePage('plugins');
		expect(get(activeView)).toMatchObject({ type: 'settings', itemId: 'category=plugins' });
	});

	it('route a restored plugins view to the Settings panel, not Build', () => {
		expect(featurePanelKeyForView('plugins')).toBe('settings');
		expect(featurePanelKeyForView('settings')).toBe('settings');
		expect(featurePanelKeyForView('mcp')).toBe('build');
	});
});
