/**
 * General: Appearance, Conversation, Thinking and tools, Region.
 *
 * F0 ships Appearance and Region as the worked example (GeneralPage.svelte
 * renders a `<SettingRow id>` for each entry here). Slice A adds the rest.
 */
import type { SettingEntry } from '../types';

export const entries: SettingEntry[] = [
	{
		id: 'general.theme',
		category: 'general',
		section: 'Appearance',
		label: 'Theme',
		keywords: ['dark', 'light', 'mode', 'color scheme', 'system']
	},
	{
		id: 'general.themeStyle',
		category: 'general',
		section: 'Appearance',
		label: 'Theme style',
		keywords: ['preset', 'colors', 'palette', 'swatches'],
		default: ''
	},
	{
		id: 'general.timezone',
		category: 'general',
		section: 'Region',
		label: 'Time zone',
		keywords: ['timezone', 'clock', 'region', 'utc'],
		default: 'local'
	}
];
